"""The append-only research event log.

Every methodology record is an event: a registration, a lock, an exposure,
a preregistration, a result, an invalidation. Nothing is edited in place —
a correction is a later event that says what it corrects — so the history
of what research did, and in what order, cannot be rewritten to look better
than it was.

Tamper evidence, not tamper proofing. Each event's hash covers its content
and the previous event's hash, so a row changed outside this module breaks
the chain from that row on, and `verify` names it. The ORM refuses updates
and deletes outright; a raw SQL edit is what the chain is for.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from ..data import schema
from ..models import ResearchEvent

GENESIS = "0" * 64

REGISTRY = "registry"
HOLDOUT = "holdout"
TRIALS = "trials"
STREAMS = (REGISTRY, HOLDOUT, TRIALS)


class LogUnavailable(RuntimeError):
    """The database has no research_events table (migration 0010 absent)."""


class TamperedLog(RuntimeError):
    """The hash chain does not verify."""


class ImmutableEvent(RuntimeError):
    """An attempt to update or delete a research event."""


class Conflict(RuntimeError):
    """The log moved on since the caller read it, or a once-only event exists.

    Raised instead of appending, so a decision made on a stale read — "the
    holdout is unconsumed" — can never be written after someone else acted.
    """


def _refuse(kind):
    def listener(mapper, connection, target):
        raise ImmutableEvent(
            f"research event {target.seq} cannot be {kind}; append a "
            "correcting event instead")
    return listener


event.listen(ResearchEvent, "before_update", _refuse("updated"))
event.listen(ResearchEvent, "before_delete", _refuse("deleted"))


def canonical(value) -> str:
    """The one serialisation hashes are taken over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str,
                      allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


CONSUMED = "holdout_consumed"


def _hash(*, seq, stream, event_type, subject, payload, created_at, prev_hash,
          consumed_lock_id=None) -> str:
    body = {"seq": seq, "stream": stream, "event_type": event_type,
            "subject": subject, "payload": payload,
            "created_at": _utc(created_at).isoformat(), "prev_hash": prev_hash}
    if consumed_lock_id is not None:
        body["consumed_lock_id"] = consumed_lock_id
    return digest(body)


@dataclass(frozen=True)
class Event:
    seq: int
    stream: str
    event_type: str
    subject: str
    payload: dict
    created_at: datetime
    event_hash: str


def available(db: Session) -> bool:
    return schema.has_table(db, ResearchEvent.__tablename__)


def _require(db: Session) -> None:
    if not available(db):
        raise LogUnavailable(
            "research_events is absent — migration 0010 has not been applied "
            "to this database")


def head(db: Session) -> tuple[int, str]:
    """The last (seq, hash) in the log, or (0, GENESIS) when it is empty."""
    _require(db)
    last = db.scalars(select(ResearchEvent).order_by(ResearchEvent.seq.desc())
                      .limit(1)).first()
    return (last.seq, last.event_hash) if last else (0, GENESIS)


def append(db: Session, *, stream: str, event_type: str, subject: str,
           payload: dict, now: datetime | None = None,
           expect_head: int | None = None) -> Event:
    """Append one event and flush it. The caller commits.

    `expect_head` makes the append a compare-and-set: it is refused unless
    the log's last event is still that one. Together with the unique `seq`
    that closes the gap between reading the log and writing to it — two
    writers who read the same head cannot both append after it.

    A `holdout_consumed` event always carries the generation it spends in
    `consumed_lock_id` (its subject). That column is not optional: the
    table's check constraint refuses a consumption row without it, and its
    unique index refuses a second consumption of the same generation — in
    the database, whatever any application check believed. The look-up
    below only turns that refusal into a clearer error.
    """
    _require(db)
    if stream not in STREAMS:
        raise ValueError(f"unknown research stream {stream!r}")
    # Stored exactly as hashed: a payload that does not survive a JSON round
    # trip unchanged would verify differently after being read back.
    payload = json.loads(canonical(payload))
    created = _utc(now or datetime.now(UTC))
    last_seq, prev = head(db)
    if expect_head is not None and last_seq != expect_head:
        raise Conflict(f"the research log moved from event {expect_head} to "
                       f"{last_seq} since it was read")
    consumed = subject if event_type == CONSUMED else None
    if consumed is not None and db.scalars(select(ResearchEvent.seq).where(
            ResearchEvent.consumed_lock_id == consumed)).first() is not None:
        raise Conflict(f"holdout generation {consumed!r} is already consumed")
    seq = last_seq + 1
    row = ResearchEvent(
        seq=seq, stream=stream, event_type=event_type, subject=subject,
        payload=payload, created_at=created, prev_hash=prev,
        consumed_lock_id=consumed,
        event_hash=_hash(seq=seq, stream=stream, event_type=event_type,
                         subject=subject, payload=payload, created_at=created,
                         prev_hash=prev, consumed_lock_id=consumed))
    db.add(row)
    db.flush()
    return _as_event(row)


def _as_event(row: ResearchEvent) -> Event:
    return Event(seq=row.seq, stream=row.stream, event_type=row.event_type,
                 subject=row.subject, payload=row.payload,
                 created_at=_utc(row.created_at), event_hash=row.event_hash)


def read(db: Session, stream: str | None = None) -> list[Event]:
    """Every event, in append order, after verifying the whole chain."""
    _require(db)
    verify(db)
    query = select(ResearchEvent).order_by(ResearchEvent.seq)
    if stream is not None:
        query = query.where(ResearchEvent.stream == stream)
    return [_as_event(r) for r in db.scalars(query)]


def verify(db: Session) -> int:
    """Check the chain end to end. Returns the event count; raises on a break."""
    _require(db)
    prev = GENESIS
    count = 0
    for expected_seq, row in enumerate(
            db.scalars(select(ResearchEvent).order_by(ResearchEvent.seq)), start=1):
        if row.seq != expected_seq:
            raise TamperedLog(f"sequence gap: expected {expected_seq}, found {row.seq}")
        if row.prev_hash != prev:
            raise TamperedLog(f"event {row.seq} does not follow event {row.seq - 1}")
        actual = _hash(seq=row.seq, stream=row.stream, event_type=row.event_type,
                       subject=row.subject, payload=row.payload,
                       created_at=row.created_at, prev_hash=row.prev_hash,
                       consumed_lock_id=row.consumed_lock_id)
        if actual != row.event_hash:
            raise TamperedLog(f"event {row.seq} was altered after it was written")
        prev = row.event_hash
        count += 1
    return count


def count(db: Session) -> int:
    _require(db)
    return int(db.scalar(select(func.count()).select_from(ResearchEvent)) or 0)
