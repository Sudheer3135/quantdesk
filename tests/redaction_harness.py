"""The real application, plus two routes that fail while quoting a URL that
carries a secret — for tests/test_log_redaction.py, which serves this with
uvicorn exactly as scripts/start.sh serves app.main (Pass 2E-B).

Not collected by pytest (no test_ prefix). Every secret comes from the
environment the test sets; none is real.
"""
import os

from app.main import app
from fastapi import WebSocket

_LEAK_URL = os.environ["QD_TEST_LEAK_URL"]


@app.get("/test-only/boom")
def boom() -> None:
    raise RuntimeError(f"upstream request failed: {_LEAK_URL}")


@app.websocket("/test-only/ws-boom")
async def ws_boom(ws: WebSocket) -> None:
    await ws.accept()
    raise RuntimeError(f"socket upstream failed: {_LEAK_URL}")
