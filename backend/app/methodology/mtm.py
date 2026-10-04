"""The daily marked-to-market research ledger (OS-7).

One row per eligible session, including the sessions nothing happened on,
built only from a *sequential execution stream*: fills that open and close
positions an account could actually have held. It is not built from the
signal-outcome evaluator, whose rows are hypothetical and overlap — five
signals an hour apart, each replayed as if it were the only trade — and
summing those into a curve would describe a portfolio nobody could hold.
A stream that overlaps, or that is made of evaluator outcomes, is refused.

Per session:

  realised_pnl       fill-to-fill P&L of positions closed that session. The
                     fills already carry the friction, so this is net of it
  execution_friction what spread and slippage took (already inside the fills;
                     reported so it can be seen, not subtracted twice)
  fees               brokerage and statutory charges booked that session
  unrealised_pnl     open positions at the session mark

**Two equity curves (Pass 2D.2).** They answer different questions and are
never mixed:

  continuity_*_equity  the economic account: every engine execution carried
                       chronologically, including executions on excluded
                       sessions. cash + unrealised, reconciling exactly to
                       starting + realised − fees + Δunrealised. It is NOT
                       a research-performance curve.
  clean_*_equity       the research-performance base: the starting equity
                       plus the P&L of *scored* sessions only. Session
                       return, drawdown, the sample counts, the bootstrap
                       and benchmark comparison read this and the scored
                       fields, nothing else.

A session that is not scored — excluded by the grid, of unknown quality,
MTM_UNAVAILABLE, or opening on marks an excluded session set — adds
nothing to the clean curve, so its P&L cannot move the denominator of any
later session's return. Its own performance fields are None, never zero.

**Marks.** A position still open at the session mark (15:30 IST) needs a
mark observed that session, at or before the mark time, and available by
then. Nothing else is used — no forward-fill from an earlier session, no
later quote, no zero. Without a permissible mark the session is
MTM_UNAVAILABLE and its equity is unknown, not guessed.

**Quality.** A session excluded by the Pass 2C grid is EXCLUDED_DATA_QUALITY,
and one with no session-quality record is QUALITY_UNKNOWN: present in the
ledger, with no scored P&L and no return — never a zero-return trading day.
If the sequential account nevertheless executed on it (the engine reads
grid-quarantined bars, and a session with a bar quarantined is still
excluded as a whole), the money is booked on the continuity curve only,
and the row says how many executions it holds.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime

import pandas as pd

from .. import market_hours
from ..data import clock_grid

OK = "OK"
MTM_UNAVAILABLE = "MTM_UNAVAILABLE"
EXCLUDED = "EXCLUDED_DATA_QUALITY"
QUALITY_UNKNOWN = "QUALITY_UNKNOWN"
CLEAN = "clean"
FLAT_BASIS = "no_open_positions"
SESSION_MARK = market_hours.MARKET_CLOSE
RECONCILE_TOLERANCE = 0.01


class LedgerError(ValueError):
    """The input is not a legitimate sequential execution stream."""


@dataclass(frozen=True)
class Fill:
    position_id: str
    kind: str                   # "open" | "close"
    time: datetime
    side: str                   # the position's direction: "BUY" long, "SELL" short
    quantity: int
    price: float                # the actual fill, friction included
    instrument: str = "NIFTY"
    friction: float = 0.0       # money, already inside `price`
    fees: float = 0.0           # money, booked on this fill
    # Where `price` came from. A reconstructed price is labelled as such and
    # never upgrades a trade's evidence.
    price_basis: str = "observed_fill"
    # How `fees` is evidenced: its brokerage/statutory split reconciled, or
    # only the total was recorded (the split is then not claimed verified).
    fee_basis: str | None = None


@dataclass(frozen=True)
class Mark:
    price: float
    observed_at: datetime
    available_at: datetime
    basis: str


MarkSource = Callable[[str, str, pd.Timestamp], Mark | None]


def _ts(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise LedgerError(f"timestamp {value!r} has no timezone")
    return stamp.tz_convert("UTC")


def _session(stamp: pd.Timestamp) -> str:
    return stamp.tz_convert(clock_grid.IST).date().isoformat()


def _mark_time(session: str) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(date.fromisoformat(session), SESSION_MARK),
                        tz=clock_grid.IST).tz_convert("UTC")


def _validate(fills: Iterable, max_concurrent: int) -> list[Fill]:
    rows = list(fills)
    for row in rows:
        if hasattr(row, "signal_id"):
            raise LedgerError("signal-outcome evaluator rows are hypothetical and "
                              "overlapping; they are not a position ledger")
        if not isinstance(row, Fill):
            raise LedgerError(f"expected Fill, got {type(row).__name__}")
    last = None
    open_now: dict[str, Fill] = {}
    for row in rows:
        stamp = _ts(row.time)
        if last is not None and stamp < last:
            raise LedgerError("fills are not in time order")
        last = stamp
        if row.quantity <= 0 or int(row.quantity) != row.quantity:
            raise LedgerError("fill quantity must be a positive integer")
        if row.side not in ("BUY", "SELL") or row.kind not in ("open", "close"):
            raise LedgerError("fill side must be BUY/SELL and kind open/close")
        if row.friction < 0 or row.fees < 0:
            raise LedgerError("friction and fees are costs and cannot be negative")
        if row.kind == "open":
            if row.position_id in open_now:
                raise LedgerError(f"position {row.position_id} opened twice")
            if len(open_now) >= max_concurrent:
                raise LedgerError("overlapping positions: this is not a sequential "
                                  "execution stream")
            open_now[row.position_id] = row
        else:
            opened = open_now.pop(row.position_id, None)
            if opened is None:
                raise LedgerError(f"position {row.position_id} closed without opening")
            if (opened.side, opened.quantity, opened.instrument) != \
                    (row.side, row.quantity, row.instrument):
                raise LedgerError(f"position {row.position_id} closed on different terms")
    return rows


def build(fills: Iterable, *, sessions: dict[str, str], starting_equity: float,
          marks: MarkSource | None = None, max_concurrent: int = 1) -> list[dict]:
    """The ledger. `sessions` maps every research session to its quality:
    CLEAN, EXCLUDED_DATA_QUALITY or QUALITY_UNKNOWN (`session_status`)."""
    rows = _validate(fills, max_concurrent)
    by_session: dict[str, list[Fill]] = {}
    for row in rows:
        day = _session(_ts(row.time))
        if day not in sessions:
            raise LedgerError(f"fill on {day}, which is not a research session")
        by_session.setdefault(day, []).append(row)

    # The continuity account.
    cash = float(starting_equity)
    equity: float | None = float(starting_equity)
    prior_unrealised = 0.0
    open_pos: dict[str, Fill] = {}
    # The clean research-performance curve: scored session P&L only.
    clean_equity = float(starting_equity)
    clean_peak = clean_equity
    carried_from_unscored = False
    ledger = []

    for day in sorted(sessions):
        quality = sessions[day]
        excluded = quality != CLEAN
        todays = by_session.get(day, [])
        if excluded and not todays and not open_pos:
            ledger.append({
                "session": day, "status": quality, "data_quality_status": quality,
                "scored": False, "unscored_reason": quality,
                "continuity_starting_equity": equity,
                "continuity_ending_equity": equity,
                "clean_starting_equity": None, "clean_ending_equity": None,
                "realised_pnl": None, "unrealised_pnl": None,
                "execution_friction": None, "fees": None, "cash": None,
                "exposure": None, "open_position_count": 0,
                "mark_basis": None, "marks": [],
                "session_pnl": None, "session_return": None, "drawdown": None,
                "trades_closed": 0})
            continue

        start = equity
        opened_with_positions = bool(open_pos)
        realised = friction = fees = 0.0
        closed = 0
        for row in todays:
            friction += row.friction
            fees += row.fees
            cash -= row.fees
            if row.kind == "open":
                open_pos[row.position_id] = row
            else:
                opened = open_pos.pop(row.position_id)
                direction = 1 if opened.side == "BUY" else -1
                pnl = (row.price - opened.price) * direction * row.quantity
                realised += pnl
                cash += pnl
                closed += 1

        at = _mark_time(day)
        unrealised: float | None = 0.0
        exposure: float | None = 0.0
        taken, bases = [], set()
        for pid, opened in sorted(open_pos.items()):
            mark = marks(opened.instrument, pid, at) if marks else None
            refusal = None
            if mark is None:
                refusal = "no mark"
            else:
                observed, available = _ts(mark.observed_at), _ts(mark.available_at)
                if available > at or observed > at:
                    refusal = "mark from after the session mark (future quote)"
                elif _session(observed) != day:
                    refusal = "mark from an earlier session (forward-fill)"
            if refusal:
                taken.append({"position_id": pid, "status": MTM_UNAVAILABLE,
                              "reason": refusal})
                unrealised = exposure = None
                continue
            direction = 1 if opened.side == "BUY" else -1
            value = (mark.price - opened.price) * direction * opened.quantity
            if unrealised is not None:
                unrealised += value
                exposure += abs(mark.price * opened.quantity)
            bases.add(mark.basis)
            taken.append({"position_id": pid, "status": OK, "mark_price": mark.price,
                          "observed_at": _ts(mark.observed_at).isoformat(),
                          "available_at": _ts(mark.available_at).isoformat(),
                          "mark_basis": mark.basis})

        if unrealised is None:
            ending = None
            status, basis = MTM_UNAVAILABLE, MTM_UNAVAILABLE
        else:
            ending = cash + unrealised
            status = OK
            basis = FLAT_BASIS if not open_pos else ",".join(sorted(bases))
            if start is not None:
                identity = start + realised - fees + (unrealised - prior_unrealised)
                if abs(identity - ending) > RECONCILE_TOLERANCE:
                    raise LedgerError(f"{day}: ledger does not reconcile "
                                      f"({identity} vs {ending})")
        session_pnl = (ending - start) if (ending is not None and start is not None) else None

        common = {
            "session": day, "continuity_starting_equity": start,
            "continuity_ending_equity": None if ending is None else round(ending, 2),
            "realised_pnl": round(realised, 2),
            "unrealised_pnl": None if unrealised is None else round(unrealised, 2),
            "execution_friction": round(friction, 2), "fees": round(fees, 2),
            "cash": round(cash, 2),
            "exposure": None if exposure is None else round(exposure, 2),
            "open_position_count": len(open_pos), "mark_basis": basis, "marks": taken,
            "trades_closed": closed,
            "fill_price_bases": sorted({f.price_basis for f in todays}),
            "fee_bases": sorted({f.fee_basis for f in todays if f.fee_basis})}

        # Scored only if the session is clean, its P&L is measured, and it
        # did not open on positions last marked by a session that is not
        # scored — that mark would carry the unscored session into it.
        reason = (quality if excluded else
                  MTM_UNAVAILABLE if session_pnl is None else
                  "opened on marks from an unscored session"
                  if opened_with_positions and carried_from_unscored else None)
        if reason is None:
            clean_start = clean_equity
            clean_equity = clean_start + session_pnl
            clean_peak = max(clean_peak, clean_equity)
            ledger.append(common | {
                "status": status, "data_quality_status": CLEAN, "scored": True,
                "unscored_reason": None,
                "clean_starting_equity": round(clean_start, 2),
                "clean_ending_equity": round(clean_equity, 2),
                "session_pnl": round(session_pnl, 2),
                "session_return": session_pnl / clean_start if clean_start else None,
                "drawdown": clean_equity / clean_peak - 1 if clean_peak else None})
        else:
            row = common | {
                "status": quality if excluded else status,
                "data_quality_status": quality, "scored": False,
                "unscored_reason": reason,
                "clean_starting_equity": None, "clean_ending_equity": None,
                "session_pnl": None, "session_return": None, "drawdown": None}
            if excluded:
                row |= {"executions_on_excluded_session": len(todays),
                        "note": "continuity only: executions on a session that is not "
                                "research-clean are booked on the continuity curve and "
                                "have no influence on the clean curve or any statistic"}
            ledger.append(row)
        carried_from_unscored = reason is not None
        equity = ending
        prior_unrealised = unrealised if unrealised is not None else prior_unrealised
    return ledger


def scored(ledger: Iterable[dict]) -> list[dict]:
    """The rows every research statistic is computed from, and only these."""
    return [r for r in ledger if r.get("scored")]


def clean_statistics(ledger: Iterable[dict]) -> dict:
    """Session-level performance from scored fields only.

    Nothing from the continuity curve and nothing from an unscored row
    enters: changing an excluded session's P&L changes none of these.
    """
    rows = scored(ledger)
    pnl = [r["session_pnl"] for r in rows]
    returns = [r["session_return"] for r in rows if r["session_return"] is not None]
    drawdowns = [r["drawdown"] for r in rows if r["drawdown"] is not None]
    return {
        "basis": "clean research equity: scored sessions only",
        "scored_sessions": len(rows),
        "unscored_sessions": sum(1 for r in ledger if not r.get("scored")),
        "positive_sessions": sum(1 for p in pnl if p > 0),
        "negative_sessions": sum(1 for p in pnl if p < 0),
        "flat_sessions": sum(1 for p in pnl if p == 0),
        "net_pnl": round(sum(pnl), 2),
        "mean_session_pnl": sum(pnl) / len(pnl) if pnl else None,
        "mean_session_return": sum(returns) / len(returns) if returns else None,
        "max_drawdown": min(drawdowns) if drawdowns else None,
        "ending_clean_equity": rows[-1]["clean_ending_equity"] if rows else None,
    }


def session_outcomes(ledger: Iterable[dict], trades: Iterable) -> list:
    """Scored sessions as `sample.SessionOutcome`, for adequacy and the
    session bootstrap. A trade counts on the session it closed, and only if
    that session is scored."""
    from .sample import SessionOutcome

    by_exit: dict[str, list] = {}
    for t in trades:
        by_exit.setdefault(_session(_ts(t.exit_time)), []).append(t)
    return [SessionOutcome(session=r["session"], net_pnl=r["session_pnl"],
                           trade_r=tuple(t.r_multiple for t in by_exit.get(r["session"], [])),
                           trade_pnl=tuple(t.net_pnl for t in by_exit.get(r["session"], [])))
            for r in scored(ledger)]


def compare_to_benchmark(ledger: Iterable[dict], benchmark: Iterable[dict]) -> dict:
    """Strategy minus benchmark, session by session, on scored sessions only.

    A benchmark session that is unavailable, or a strategy session that is
    not scored, is left out of the comparison and counted, never zeroed.
    """
    bench = {b["session"]: b.get("session_return") for b in benchmark
             if b.get("status", "ok") == "ok"}
    paired = [(r["session"], r["session_return"], bench[r["session"]])
              for r in scored(ledger)
              if r["session_return"] is not None and bench.get(r["session"]) is not None]
    excess = [s - b for _, s, b in paired]
    return {"basis": "scored sessions only", "paired_sessions": len(paired),
            "mean_excess_return": sum(excess) / len(excess) if excess else None,
            "sessions_beating_benchmark": sum(1 for x in excess if x > 0)}


# Every engine money figure is rounded to the paisa, so an identity over
# four of them can be off by up to two paise without anything being wrong.
MONEY_TOLERANCE = 0.02
EXACT_FILL = "engine_unrounded_fill"
RECONSTRUCTED = "reconstructed_from_accounting"
FEES_RECONCILED = "components_reconciled"
FEES_TOTAL_ONLY = "total_only"


def fee_basis(n: int, trade) -> str:
    """How a trade's fees are evidenced. Absent is not zero.

      both components recorded   brokerage + statutory must equal fees —
                                 so recorded zeros against fees of 40 fail
      neither recorded (None)    the total alone is accepted and labelled
                                 `total_only`; the split is not claimed
      only one recorded          refused: there is no representation for
                                 an unrecorded remainder
    """
    brokerage = getattr(trade, "brokerage", None)
    statutory = getattr(trade, "statutory_fees", None)
    if brokerage is None and statutory is None:
        return FEES_TOTAL_ONLY
    if brokerage is None or statutory is None:
        raise LedgerError(f"engine trade {n} does not reconcile: fee split is partial "
                          f"(brokerage {brokerage}, statutory {statutory})")
    if brokerage < 0 or statutory < 0:
        raise LedgerError(f"engine trade {n} does not reconcile: negative fee component")
    if abs(brokerage + statutory - trade.fees) > MONEY_TOLERANCE:
        raise LedgerError(f"engine trade {n} does not reconcile: brokerage {brokerage} "
                          f"+ statutory {statutory} ≠ fees {trade.fees}")
    return FEES_RECONCILED


def check_engine_trade(n: int, trade) -> tuple[float, float, str]:
    """Independently reconcile one engine trade. Returns (entry, exit, basis).

    Every check stands on its own; none is satisfied by deriving a number
    from another:

      side, entry_side, exit_side   consistent (a long buys then sells)
      quantity                      a positive whole number
      timestamps                    timezone-aware, exit not before entry
      friction, fees                non-negative
      gross − friction − fees       equals net
      brokerage + statutory         equals fees when both are recorded;
                                    neither recorded is `total_only`, one
                                    alone is refused (`fee_basis`)
      fill prices                   (exit − entry) × direction × qty equals
                                    gross − friction: the direction and size
                                    of the move agree with the money

    Prices come from the engine's unrounded fills where it recorded them.
    Only a trade without them falls back to a price rebuilt from its
    accounting — labelled `reconstructed_from_accounting`, and only after
    every check above has passed on the displayed prices.
    """
    if hasattr(trade, "signal_id"):
        raise LedgerError("signal-outcome evaluator rows are hypothetical and "
                          "overlapping; they are not a position ledger")

    def refuse(why: str):
        raise LedgerError(f"engine trade {n} does not reconcile: {why}")

    if trade.side not in ("BUY", "SELL"):
        refuse(f"side {trade.side!r}")
    opposite = "SELL" if trade.side == "BUY" else "BUY"
    if (trade.entry_side or trade.side) != trade.side or \
            (trade.exit_side or opposite) != opposite:
        refuse(f"sides {trade.side}/{trade.entry_side}/{trade.exit_side} disagree")
    if not isinstance(trade.quantity, int) or isinstance(trade.quantity, bool) \
            or trade.quantity <= 0:
        refuse(f"quantity {trade.quantity!r}")
    opened, closed = pd.Timestamp(trade.entry_time), pd.Timestamp(trade.exit_time)
    if opened.tzinfo is None or closed.tzinfo is None:
        refuse("timestamps without a timezone")
    if closed < opened:
        refuse("exit before entry")
    if trade.execution_friction < 0 or trade.fees < 0:
        refuse("negative friction or fees")
    if abs(trade.gross_pnl - trade.execution_friction - trade.fees - trade.net_pnl) \
            > MONEY_TOLERANCE:
        refuse(f"gross {trade.gross_pnl} − friction {trade.execution_friction} − fees "
               f"{trade.fees} ≠ net {trade.net_pnl}")
    fee_basis(n, trade)

    direction = 1 if trade.side == "BUY" else -1
    at_fills = trade.gross_pnl - trade.execution_friction
    exact_entry = getattr(trade, "entry_fill_exact", None)
    exact_exit = getattr(trade, "exit_fill_exact", None)
    if exact_entry is not None and exact_exit is not None:
        if abs(exact_entry - trade.entry) > 0.006 or abs(exact_exit - trade.exit) > 0.006:
            refuse("unrounded fills disagree with the displayed prices")
        moved = (exact_exit - exact_entry) * direction * trade.quantity
        if abs(moved - at_fills) > MONEY_TOLERANCE:
            refuse(f"fills move {moved:.4f}, the money says {at_fills:.2f}")
        return float(exact_entry), float(exact_exit), EXACT_FILL

    # No unrounded evidence: the displayed prices can be off by half a paisa
    # each, so the move is checked within what that rounding explains.
    moved = (trade.exit - trade.entry) * direction * trade.quantity
    if abs(moved - at_fills) > MONEY_TOLERANCE + 0.01 * trade.quantity:
        refuse(f"displayed fills move {moved:.2f}, the money says {at_fills:.2f}")
    rebuilt = trade.entry + (trade.net_pnl + trade.fees) / (direction * trade.quantity)
    return float(trade.entry), float(rebuilt), RECONSTRUCTED


def fills_from_engine(trades: Iterable) -> list[Fill]:
    """The sequential backtest engine's trades as a fill stream.

    Each trade opens at its entry fill and closes at its exit fill; the
    engine books friction and fees on the round trip at the close, so they
    go on the closing fill. Every trade is reconciled independently first
    (`check_engine_trade`) and a trade whose figures disagree is refused.
    """
    fills = []
    for n, trade in enumerate(trades, start=1):
        entry, exit_, basis = check_engine_trade(n, trade)
        fees_evidence = fee_basis(n, trade)
        pid = f"engine-{n}"
        fills.append(Fill(position_id=pid, kind="open", time=pd.Timestamp(trade.entry_time),
                          side=trade.side, quantity=trade.quantity, price=entry,
                          price_basis=EXACT_FILL if basis == EXACT_FILL else
                          "engine_displayed_fill"))
        fills.append(Fill(position_id=pid, kind="close", time=pd.Timestamp(trade.exit_time),
                          side=trade.side, quantity=trade.quantity, price=exit_,
                          friction=trade.execution_friction, fees=trade.fees,
                          price_basis=basis, fee_basis=fees_evidence))
    return fills


def session_status(candles: pd.DataFrame, timeframe: str = "5m") -> dict[str, str]:
    """The `sessions` map for `build`, from the one session-quality source.

    A session whose raw bars failed the Pass 2C grid is EXCLUDED even after
    its bad rows were quarantined and the frame was concatenated; one with
    no session-quality record is QUALITY_UNKNOWN (`clock_grid.session_quality`).
    Neither is scored.
    """
    status = {clock_grid.CLEAN_SESSION: CLEAN, clock_grid.FAULTY_SESSION: EXCLUDED}
    return {day: status.get(verdict["quality"], QUALITY_UNKNOWN)
            for day, verdict in clock_grid.session_quality(candles, timeframe).items()}


def index_marks(candles: pd.DataFrame, timeframe_minutes: int = 5) -> MarkSource:
    """Marks from the last completed index bar of the same session, if any."""
    stamps = pd.to_datetime(candles["timestamp"], utc=True)
    closes = stamps + pd.Timedelta(minutes=timeframe_minutes)
    frame = pd.DataFrame({"close_time": closes, "close": candles["close"].to_numpy()})

    def source(instrument: str, position_id: str, at: pd.Timestamp) -> Mark | None:
        done = frame[(frame["close_time"] <= at)
                     & (frame["close_time"].dt.tz_convert(clock_grid.IST).dt.date
                        == at.tz_convert(clock_grid.IST).date())]
        if done.empty:
            return None
        last = done.iloc[-1]
        return Mark(price=float(last["close"]), observed_at=last["close_time"],
                    available_at=last["close_time"],
                    basis="index_last_completed_bar_close")
    return source


def as_dicts(fills: Iterable[Fill]) -> list[dict]:
    return [asdict(f) for f in fills]
