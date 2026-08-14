"""Shared test fixtures.

The database fixtures run against in-memory SQLite. That is a deliberate
trade: it keeps `pytest tests` a single command with no services to start,
which is the difference between a suite that runs on every change and one
that runs before a release.

What it does not do is prove the Postgres path. Two divergences matter and
both are handled rather than ignored:

  - SQLite has no native timezone-aware timestamp. It stores what you give
    it and hands back a naive datetime. Everything in this codebase writes
    UTC, and `repository.load_index_candles` re-localises on read, so the
    round trip is correct — but a test asserting on `tzinfo` straight off a
    model attribute will see None here and a real offset on Postgres.

  - SQLite is far more permissive about types. A column that would be
    rejected by Postgres can round-trip here.

`data/upsert.py` exists precisely so the write path is exercised on both,
and CI runs this suite against a real Postgres as well.
"""
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import Base  # noqa: E402

# Set TEST_DATABASE_URL to a scratch Postgres and every database test runs
# against both backends. CI does this; locally it is opt-in, because
# requiring a running server to test a parser is how a suite stops being run.
POSTGRES_URL = os.getenv("TEST_DATABASE_URL")
BACKENDS = ["sqlite"] + (["postgresql"] if POSTGRES_URL else [])


@pytest.fixture(params=BACKENDS)
def engine(request):
    """A clean database per test, on each configured backend.

    SQLite is in-memory. StaticPool keeps every checkout on the same
    connection — without it each connection gets its own blank `:memory:`
    database and the tables vanish between statements.
    """
    if request.param == "sqlite":
        eng = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )
    else:
        eng = create_engine(POSTGRES_URL, future=True)
        # Previous runs may have died mid-test. Start from nothing rather
        # than inheriting rows that would make counts mysteriously wrong.
        Base.metadata.drop_all(eng)

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        if request.param != "sqlite":
            Base.metadata.drop_all(eng)
        eng.dispose()


@pytest.fixture
def db(engine):
    with Session(engine, expire_on_commit=False) as session:
        yield session
