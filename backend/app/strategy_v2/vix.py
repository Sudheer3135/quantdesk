"""India VIX history: fetched once from Angel, then kept current live.

The gate ranks today's VIX against about a year of daily closes. Angel's
`getCandleData` serves that history for the VIX index token the same way it
serves NIFTY's, so one backfill fills the table; after that the paper trader
records each session's close from the live socket and no further history
request is needed.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data import angel_history as ah
from ..models import VixDaily

log = logging.getLogger(__name__)

INTERVAL_DAY = "ONE_DAY"
# Angel allows far longer daily windows than intraday ones; a year per
# request keeps each response small and each failure cheap.
WINDOW_DAYS = 365

SOURCE_HISTORY = "angel_hist"
SOURCE_LIVE = "angel_live"


def load_closes(db: Session, *, before: date, limit: int = 400) -> list[float]:
    """Daily closes strictly before `before`, oldest first."""
    rows = db.scalars(
        select(VixDaily).where(VixDaily.session_date < before)
        .order_by(VixDaily.session_date.desc()).limit(limit)).all()
    return [r.close for r in reversed(rows)]


def coverage(db: Session) -> dict:
    rows = db.scalars(select(VixDaily).order_by(VixDaily.session_date)).all()
    if not rows:
        return {"sessions": 0, "first": None, "last": None}
    return {"sessions": len(rows), "first": rows[0].session_date.isoformat(),
            "last": rows[-1].session_date.isoformat(), "last_close": rows[-1].close}


def store(db: Session, frame: pd.DataFrame, source: str) -> dict:
    """Insert sessions the table does not hold. Never restates one."""
    if frame.empty:
        return {"inserted": 0, "skipped_existing": 0}
    days = frame["session_date"].tolist()
    held = set(db.scalars(select(VixDaily.session_date).where(
        VixDaily.session_date.in_(days))).all())
    inserted = 0
    for row in frame.itertuples(index=False):
        if row.session_date in held or not row.close or row.close <= 0:
            continue
        db.add(VixDaily(session_date=row.session_date, open=row.open, high=row.high,
                        low=row.low, close=float(row.close), source=source))
        inserted += 1
    db.commit()
    return {"inserted": inserted, "skipped_existing": len(held)}


def record_close(db: Session, day: date, value: float, *, source: str = SOURCE_LIVE) -> bool:
    """File one session's close from the live feed, unless it is already there."""
    if value is None or value <= 0:
        return False
    if db.scalar(select(VixDaily.id).where(VixDaily.session_date == day)):
        return False
    db.add(VixDaily(session_date=day, close=float(value), source=source))
    db.commit()
    return True


def to_daily(frame: pd.DataFrame) -> pd.DataFrame:
    """Angel's daily candles, keyed by the IST session they describe."""
    if frame.empty:
        return pd.DataFrame(columns=["session_date", "open", "high", "low", "close"])
    out = frame.copy()
    out["session_date"] = out["timestamp"].dt.tz_convert(ah.IST).dt.date
    out = out.drop_duplicates("session_date", keep="last")
    return out[["session_date", "open", "high", "low", "close"]].reset_index(drop=True)


def fetch_daily(client, token: str, start: date, end: date, *, master: list[dict],
                settings=None, sleep=time.sleep) -> pd.DataFrame:
    """Daily VIX candles from Angel, a year per request, token validated first."""
    from ..config import get_settings
    settings = settings or get_settings()
    ah.validate_token(token, master)

    frames = []
    cursor = end
    while cursor >= start:
        window_start = max(start, cursor - timedelta(days=WINDOW_DAYS - 1))
        params = {
            "exchange": "NSE", "symboltoken": str(token), "interval": INTERVAL_DAY,
            "fromdate": datetime.combine(window_start, datetime.min.time()
                                         ).replace(hour=9, minute=15).strftime("%Y-%m-%d %H:%M"),
            "todate": datetime.combine(cursor, datetime.min.time()
                                       ).replace(hour=15, minute=30).strftime("%Y-%m-%d %H:%M"),
        }
        call = ah._call(client, params, pace=settings.angel_history_pace_seconds,
                        max_retries=settings.angel_history_max_retries, sleep=sleep)
        frames.append(ah._to_frame(call["response"].get("data") or []))
        cursor = window_start - timedelta(days=1)

    frames = [f for f in frames if not f.empty]
    if not frames:
        return to_daily(pd.DataFrame(columns=list(ah.CANDLE_FIELDS)))
    combined = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    return to_daily(combined)
