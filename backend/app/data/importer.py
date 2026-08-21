"""Historical importers.

Idempotent by construction: running the same import twice writes the same
rows and changes nothing. That property is what makes a backfill safe to
retry, safe to schedule, and safe to run while you are still unsure whether
the last one finished.

Every import returns an `ImportReport` rather than a row count. A count
answers "did it work?"; the report answers "what did it do?", which is the
question you actually have when a number looks wrong three weeks later.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pandas as pd
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from ..market_hours import is_open as market_is_open
from ..market_hours import is_trading_date
from ..models import CandleRecord, OptionCandle, OptionContract
from .upsert import UpsertResult, upsert
from .validation import RejectionReport, clean_candles

log = logging.getLogger(__name__)

IST = "Asia/Kolkata"

# NSE writes expiries as "07-Aug-2026".
NSE_EXPIRY_FORMAT = "%d-%b-%Y"

TIMEFRAME_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


@dataclass
class ImportReport:
    symbol: str
    timeframe: str
    source: str
    rejection: RejectionReport = field(default_factory=RejectionReport)
    write: UpsertResult = field(default_factory=UpsertResult)
    first_ts: str | None = None
    last_ts: str | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "source": self.source,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "validation": self.rejection.to_dict(),
            "write": self.write.to_dict(),
            "warnings": self.rejection.warnings(),
        }


def import_index_candles(
    db: Session,
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    source: str,
) -> ImportReport:
    """Validate, then store index candles. Safe to run repeatedly.

    `source` is a required positional argument and must stay one. It
    defaulted to "free" once, and a caller that forgot it labelled thousands
    of mock candles as real data — the single column separating trustworthy
    rows from junk became useless exactly when it mattered. A forgotten
    argument must fail loudly at the call site.
    """
    report = ImportReport(symbol=symbol, timeframe=timeframe, source=source)

    clean, report.rejection = clean_candles(df, timeframe)
    for line in report.rejection.warnings():
        log.warning("%s %s import: %s", symbol, timeframe, line)

    if clean.empty:
        return report

    # Defence in depth. `clean_candles` already drops future-dated bars, so
    # reaching here means a programming error rather than a bad feed — and a
    # future-dated row in a historical table is the one corruption that is
    # invisible afterwards, because it looks exactly like real data.
    now = pd.Timestamp.now(tz="UTC")
    if clean["timestamp"].max() > now:
        raise ValueError(
            f"refusing to import {symbol} {timeframe}: a bar dated "
            f"{clean['timestamp'].max()} survived validation, which is a bug "
            "in the validation gate, not a bad source."
        )

    ingested_at = datetime.now(UTC)
    session_dates = clean["timestamp"].dt.tz_convert(IST).dt.date
    synthetic = report.rejection.volume_is_synthetic

    rows = [
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": ts.to_pydatetime(),
            "open": float(o), "high": float(h), "low": float(low), "close": float(c),
            "volume": float(v),
            "source": source,
            "session_date": sd,
            "ingested_at": ingested_at,
            "volume_is_synthetic": synthetic,
            "revision": 0,
        }
        for ts, o, h, low, c, v, sd in zip(
            clean["timestamp"], clean["open"], clean["high"], clean["low"],
            clean["close"], clean["volume"], session_dates, strict=True,
        )
    ]

    report.write = upsert(
        db, CandleRecord, rows,
        conflict_columns=("symbol", "timeframe", "timestamp"),
        update_columns=("open", "high", "low", "close", "volume", "source",
                        "session_date", "ingested_at", "volume_is_synthetic"),
        # Referencing the table column (not `excluded`) reads the row that is
        # already there, so a restated bar is counted rather than silently
        # replaced.
        extra_set={"revision": CandleRecord.revision + 1},
    )

    report.first_ts = clean["timestamp"].min().isoformat()
    report.last_ts = clean["timestamp"].max().isoformat()
    log.info("%s %s: %s", symbol, timeframe, report.write.to_dict())
    return report


# ======================================================================
# Options
# ======================================================================

@dataclass
class OptionImportReport:
    underlying: str
    expiry: str | None = None
    timeframe: str = "5m"
    source: str = ""
    bar_timestamp: str | None = None
    strikes: int = 0
    contracts: UpsertResult = field(default_factory=UpsertResult)
    candles: UpsertResult = field(default_factory=UpsertResult)
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Set when the whole snapshot was declined, with the reason. Distinct
    # from a warning: a warning describes something stored, this describes
    # something deliberately not stored.
    refused: str | None = None

    def to_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry,
            "timeframe": self.timeframe,
            "source": self.source,
            "bar_timestamp": self.bar_timestamp,
            "strikes": self.strikes,
            "contracts": self.contracts.to_dict(),
            "candles": self.candles.to_dict(),
            "skipped": {k: v for k, v in self.skipped.items() if v},
            "warnings": self.warnings,
            "refused": self.refused,
        }


def parse_expiry(value: str | date | datetime) -> date:
    """NSE's `07-Aug-2026` into a real date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value).strip(), NSE_EXPIRY_FORMAT).date()


def bucket_start(moment: datetime, timeframe: str = "5m") -> datetime:
    """Floor a capture time to the bar it belongs in."""
    minutes = TIMEFRAME_MINUTES.get(timeframe)
    if not minutes:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    moment = moment.astimezone(UTC)
    floored = moment.replace(second=0, microsecond=0)
    return floored - pd.Timedelta(minutes=floored.minute % minutes).to_pytimedelta()


def _clean_iv(raw: float | None) -> float | None:
    """NSE publishes IV as a percentage, and 0 for anything untraded.

    Stored as a fraction, because that is what `option_pricing` takes and a
    unit mismatch here would be silent: 13.5 instead of 0.135 produces a
    premium far too large without raising anything.
    """
    if raw is None:
        return None
    value = float(raw)
    if value <= 0 or value > 300:
        return None
    return value / 100.0


def _chain_source_time(chain: pd.DataFrame) -> datetime | None:
    """When the exchange last printed this chain, if the source said so.

    NSE stamps its payload; the mock broker does not. A missing stamp is not
    an error — it means this particular source cannot corroborate the day,
    and the caller falls back to the calendar for a warning.
    """
    raw = getattr(chain, "attrs", {}).get("source_time") if chain is not None else None
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def import_option_snapshot(
    db: Session,
    chain: pd.DataFrame,
    underlying: str,
    expiry: str | date,
    spot: float,
    source: str,
    captured_at: datetime | None = None,
    timeframe: str = "5m",
    lot_size: int | None = None,
) -> OptionImportReport:
    """Fold one option-chain snapshot into the current bar.

    NSE publishes a snapshot, not a tape. There is no historical option feed
    to backfill from at any price a retail account would pay, so option
    history can only be accumulated forward — one poll at a time, from the
    day this is switched on.

    That shapes what a "bar" means here. Repeated polls inside the same
    five-minute bucket are merged: the first sets the open, the extremes
    track the high and low, the latest sets the close, and `samples` counts
    how many observations went in. A bar built from one sample has a range
    of zero and is not a candle — `bar_kind` says `snapshot` for exactly
    this reason, and nothing downstream should treat it as traded OHLC.
    """
    report = OptionImportReport(underlying=underlying, timeframe=timeframe,
                                source=source, skipped=dict.fromkeys(
                                    ("no_price", "bad_strike"), 0))

    captured_at = (captured_at or datetime.now(UTC)).astimezone(UTC)
    now = datetime.now(UTC)
    if captured_at > now:
        raise ValueError(
            f"refusing a snapshot captured at {captured_at.isoformat()}, which "
            "is in the future. Historical tables must never hold a row for a "
            "moment that has not happened."
        )

    expiry_date = parse_expiry(expiry)
    report.expiry = expiry_date.isoformat()

    bar_ts = bucket_start(captured_at, timeframe)
    report.bar_timestamp = bar_ts.isoformat()
    session_date = pd.Timestamp(bar_ts).tz_convert(IST).date()

    # ---- did the exchange actually trade on this day? -------------------
    #
    # On a holiday NSE keeps serving the previous session's chain: same
    # strikes, same prices, HTTP 200. Nothing in the payload looks wrong, so
    # the collector filed a full day of identical snapshots dated on a day
    # the market never opened — roughly 375 of them, aggregated into bars
    # whose high, low and close are all the closing price. Option history
    # cannot be backfilled *or* meaningfully cleaned once it is mixed in.
    #
    # The gate is the chain's own timestamp, not the holiday list. Evidence
    # beats a calendar: it catches unlisted closures, mid-session halts and a
    # source serving stale data, and it stays right when the calendar is
    # wrong — which matters because the 2026 list is still marked
    # provisional. Refusing a real session on a mis-transcribed holiday
    # would destroy data that can never be recovered; refusing a replay
    # costs nothing, because the replay carries no new information.
    chain_time = _chain_source_time(chain)
    if chain_time is not None:
        # `IST` here is the zone *name*, matching this module's pandas
        # idiom two lines above — not a tzinfo object.
        printed_on = pd.Timestamp(chain_time).tz_convert(IST).date()
        if printed_on != session_date:
            report.refused = (
                f"chain was last printed on {printed_on.isoformat()}, not "
                f"{session_date.isoformat()} — the exchange did not trade "
                f"today, so this snapshot is a replay of an earlier session. "
                f"Nothing stored.")
            report.warnings.append(report.refused)
            return report

    # Additive, not a replacement. A Saturday capture is both outside market
    # hours and not a trading day, and the existing contract is that an
    # out-of-hours capture says so. The calendar note adds information; it
    # does not take the older warning's place.
    if not market_is_open(captured_at):
        report.warnings.append(
            "Captured outside market hours. The chain does not change when "
            "the market is shut, so this bar repeats the closing state.")

    if not is_trading_date(session_date):
        # The calendar says this is not a session, and no chain timestamp
        # was available to corroborate it. A warning rather than a refusal:
        # the calendar alone is not worth an unrecoverable deletion, because
        # a mis-transcribed holiday would throw away a session that can
        # never be re-collected.
        report.warnings.append(
            f"{session_date.isoformat()} is not a trading day according to "
            f"the exchange calendar, and the chain carried no timestamp to "
            f"confirm it either way. Stored, but treat it as suspect.")

    if chain is None or chain.empty:
        report.warnings.append("Empty chain — nothing to store.")
        return report

    report.strikes = len(chain)

    # ---- contracts ----------------------------------------------------
    contract_rows = []
    for record in chain.itertuples():
        strike = float(getattr(record, "strike", 0) or 0)
        if strike <= 0:
            report.skipped["bad_strike"] += 1
            continue
        for kind in ("CE", "PE"):
            contract_rows.append({
                "underlying": underlying,
                "expiry_date": expiry_date,
                "strike": strike,
                "option_type": kind,
                "lot_size": lot_size,
                "tradingsymbol": f"{underlying}{expiry_date:%d%b%y}".upper()
                                 + f"{strike:.0f}{kind}",
                "first_seen": captured_at,
                "last_seen": captured_at,
                "source": source,
            })

    if not contract_rows:
        report.warnings.append("No usable strikes in the payload.")
        return report

    report.contracts = upsert(
        db, OptionContract, contract_rows,
        conflict_columns=("underlying", "expiry_date", "strike", "option_type"),
        # `first_seen` is deliberately absent: it records when this contract
        # entered the archive and must not be overwritten by a later poll.
        update_columns=("last_seen", "lot_size", "tradingsymbol"),
    )

    # ---- map contracts to ids ------------------------------------------
    ids = {
        (row.strike, row.option_type): row.id
        for row in db.execute(
            select(OptionContract.id, OptionContract.strike,
                   OptionContract.option_type)
            .where(OptionContract.underlying == underlying,
                   OptionContract.expiry_date == expiry_date)
        ).all()
    }

    # ---- candles --------------------------------------------------------
    candle_rows = []
    for record in chain.itertuples():
        strike = float(getattr(record, "strike", 0) or 0)
        if strike <= 0:
            continue
        for kind, ltp_col, oi_col, oi_chg_col, vol_col, iv_col in (
            ("CE", "call_ltp", "call_oi", "call_oi_change", "call_volume", "call_iv"),
            ("PE", "put_ltp", "put_oi", "put_oi_change", "put_volume", "put_iv"),
        ):
            price = getattr(record, ltp_col, None)
            if price is None or float(price) <= 0:
                # An untraded strike has no last price. Storing zero would
                # put a fictional premium in the archive, and a backtest
                # would happily "buy" it.
                report.skipped["no_price"] += 1
                continue

            contract_id = ids.get((strike, kind))
            if contract_id is None:
                continue

            price = float(price)
            candle_rows.append({
                "contract_id": contract_id,
                "timeframe": timeframe,
                "timestamp": bar_ts,
                "open": price, "high": price, "low": price, "close": price,
                "volume": float(getattr(record, vol_col, 0) or 0),
                "open_interest": float(getattr(record, oi_col, 0) or 0),
                "oi_change": float(getattr(record, oi_chg_col, 0) or 0),
                "iv": _clean_iv(getattr(record, iv_col, None)),
                "bid": None, "ask": None,
                "underlying_close": float(spot) if spot else None,
                "bar_kind": "snapshot",
                "source": source,
                "session_date": session_date,
                "ingested_at": now,
                "revision": 0,
                "samples": 1,
            })

    if not candle_rows:
        report.warnings.append("Every strike lacked a traded price.")
        return report

    # Merging rather than replacing. `open` is left out of the update set on
    # purpose — the first poll in a bucket opened the bar, and a later poll
    # must not rewrite history. `case` is used instead of GREATEST/LEAST
    # because Postgres and SQLite disagree on those, and `max(a, b)` is a
    # scalar in one and an aggregate in the other.
    report.candles = upsert(
        db, OptionCandle, candle_rows,
        conflict_columns=("contract_id", "timeframe", "timestamp"),
        update_columns=("close", "volume", "open_interest", "oi_change", "iv",
                        "underlying_close", "source", "ingested_at"),
        extra_set={
            "high": lambda ex: case(
                (OptionCandle.high > ex.high, OptionCandle.high), else_=ex.high),
            "low": lambda ex: case(
                (OptionCandle.low < ex.low, OptionCandle.low), else_=ex.low),
            "samples": OptionCandle.samples + 1,
            "revision": OptionCandle.revision + 1,
        },
    )

    log.info("%s %s snapshot at %s: %s", underlying, report.expiry,
             report.bar_timestamp, report.candles.to_dict())
    return report
