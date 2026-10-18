"""A stand-in API for tests/test_graceful_shutdown.py (Pass 2E-B).

Its shutdown is the real one: uvicorn's graceful shutdown, then the
lifespan runs `migration_guard.release_after_drain` with the drain time from
Settings (app.shutdown_policy) — only the writer is a stand-in, one whose
stop takes as long as the test says. Every step is appended to
$QD_TEST_MARKS with a timestamp.

  QD_TEST_WRITER_SECONDS   how long the writer's stop takes; negative: never returns
  QD_TEST_STUCK=1          the lifespan shutdown blocks forever, before any drain

Not collected by pytest (no test_ prefix).
"""
import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from app import migration_guard, shutdown_policy
from app.config import get_settings
from fastapi import FastAPI, WebSocket

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

MARKS = Path(os.environ["QD_TEST_MARKS"])
WRITER_SECONDS = float(os.environ.get("QD_TEST_WRITER_SECONDS", "0"))
STUCK = os.environ.get("QD_TEST_STUCK") == "1"


def mark(event: str) -> None:
    with MARKS.open("a") as f:
        f.write(f"{time.time():.3f} {event}\n")


class Lease:
    def release(self) -> None:
        mark("lease-released")


class SlowWriter:
    def __init__(self) -> None:
        self.running = True

    def stop(self) -> None:
        mark("writer-stop-called")
        if WRITER_SECONDS < 0:
            threading.Event().wait()                 # never returns
        # In small steps, so a process frozen part-way still has the rest of
        # its work to do once it runs again.
        for _ in range(round(WRITER_SECONDS * 10)):
            time.sleep(0.1)
        self.running = False
        mark("writer-stopped")


@asynccontextmanager
async def lifespan(app):
    mark(f"started pid={os.getpid()}")
    # As app.main does: the effective policy, checked against the deadline
    # the supervisor was given — and reported, for the test to compare.
    effective = shutdown_policy.check_supervisor(get_settings())
    mark(f"policy deadline={effective.deadline} "
         f"supervisor={os.environ.get(shutdown_policy.SUPERVISOR_ENV, 'none')}")
    try:
        yield
    finally:
        mark("shutdown-begins")
        if STUCK:
            while True:                              # only SIGKILL ends this
                time.sleep(1)
        writer = SlowWriter()
        migration_guard.release_after_drain(
            Lease(), [migration_guard.Writer("slow", writer.stop, lambda: not writer.running)],
            timeout=get_settings().shutdown_drain_seconds)
        mark("drained")


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.websocket("/ws/hold")
async def hold(ws: WebSocket) -> None:
    """Like the dashboard's socket: never reads, so only uvicorn's graceful
    shutdown timeout can end it."""
    await ws.accept()
    await asyncio.sleep(3600)
