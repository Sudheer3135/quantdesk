"""Frozen benchmark definitions and the primary research metric (OS-5).

Defined before any strategy search, and not edited afterwards. Each
definition is hashed, and `DEFINITION_HASHES` pins those hashes: changing a
definition changes its hash, the pin no longer matches, and the test that
checks it fails. A benchmark that is revised after results are seen is not
a benchmark.

  A  cash — no position, return 0 before carrying costs. A sanity floor.
  B  passive NIFTY session comparator — long the index from the open of
     the 09:15 bar to the close of the 15:25 bar, every clean session, no
     costs. An *index market comparator*, not an executable option
     strategy. Its times are the session's own boundaries, fixed here, never
     chosen by looking at results.
  C  the Quant Desk pre-optimisation baseline — the strategy exactly as
     verified at `repair-2c-verified`. A reference point for later research,
     evaluated on seen development data only; never populated by spending
     a prospective holdout.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from ..data import clock_grid
from . import events

PRIMARY_METRIC = "net_expectancy_r"
PRIMARY_METRIC_DEFINITION = ("net expectancy in R per closed trade, after the "
                             "declared execution friction and costs")
SECONDARY_METRICS = ("profit_factor", "net_pnl", "session_return", "max_drawdown",
                     "trade_count", "active_session_count")
DESCRIPTIVE_ONLY = ("win_rate",)
METRIC_POLICY = {
    "primary_metric": PRIMARY_METRIC,
    "definition": PRIMARY_METRIC_DEFINITION,
    "secondary_metrics": list(SECONDARY_METRICS),
    "descriptive_only": list(DESCRIPTIVE_ONLY),
    "rule": "win rate is descriptive; no strategy is selected on it alone. A "
            "study may override the primary metric only before it has results.",
}


@dataclass(frozen=True)
class Benchmark:
    benchmark_id: str
    name: str
    kind: str
    definition: str
    label: str
    parameters: tuple

    def to_dict(self) -> dict:
        return asdict(self) | {"definition_hash": self.definition_hash}

    @property
    def definition_hash(self) -> str:
        return events.digest(asdict(self))


CASH = Benchmark(
    benchmark_id="A_cash_v1", name="Cash / no trade", kind="cash",
    definition="no positions; session return 0 before carrying costs",
    label="sanity baseline", parameters=())

PASSIVE_NIFTY = Benchmark(
    benchmark_id="B_passive_nifty_session_v1",
    name="Passive NIFTY session comparator", kind="index_comparator",
    definition="long NIFTY 50 at the open of the 09:15 IST bar, flat at the close "
               "of the 15:25 IST bar, every clean session; no costs; return = "
               "close / open - 1; sessions excluded for data quality are "
               "unavailable, never zero",
    label="index market comparator — not an executable option strategy",
    parameters=(("entry_bar", "09:15"), ("exit_bar", "15:25"), ("timeframe", "5m"),
                ("costs", "none")))

PRE_OPTIMISATION_BASELINE = Benchmark(
    benchmark_id="C_quantdesk_repair_2c_baseline_v1",
    name="Quant Desk pre-optimisation baseline", kind="strategy_reference",
    definition="the index signal engine and backtest engine exactly as verified "
               "at tag repair-2c-verified, run sequentially with the default "
               "execution policy and flat costs below",
    label="pre-optimisation reference; seen development data only",
    parameters=(
        ("git_tag", "repair-2c-verified"),
        ("git_commit", "09c6ffc2eb1d0ce99da55ba1f2d659a93e8a673e"),
        ("code_id", "09c6ffc2eb1d"),
        ("strategy_version", "nifty-signal-engine/1"),
        # signal_engine.parameters("5m", rr_target=2.0, atr_stop_multiple=1.2)
        ("parameter_hash",
         "49419f9bdc2c8039e65a87dacb140d6ea640e98f7e09b15f896c843752d55dbe"),
        # ExecutionPolicy().describe(): zero latency, keep planned levels, stop first
        ("execution_policy_hash",
         "c39356640c4fb4682fb62b80282fc200dcc93370c81abee8a9370594177a799e"),
        # describe(FlatCostModel(120.0), SlippageModel(index_pct=0.02))
        ("cost_model_hash",
         "e976782fa70e772cfa46e16d4fc0ad1a09e64e381a725b8057d35bc3975bf130"),
        ("cost_per_round_trip", 120.0), ("slippage_index_pct", 0.02),
        ("warmup_bars", 60), ("analysis_window", 300), ("max_bars_in_trade", 24)))

BENCHMARKS = {b.benchmark_id: b for b in (CASH, PASSIVE_NIFTY, PRE_OPTIMISATION_BASELINE)}

# The pinned hashes. Editing a definition above breaks this pin, which is
# the point; a new definition needs a new benchmark_id.
DEFINITION_HASHES = {
    "A_cash_v1": "39f5b3fafbfcb7b8f99ba1ceee3d14c65824fb33b011132ff4c4071171dee2c5",
    "B_passive_nifty_session_v1":
        "34cde2dc58490c6a5f5b10916eb16db4e10a7a214a7d960411a612f272244086",
    "C_quantdesk_repair_2c_baseline_v1":
        "42bb68d099a5b1b994d882ec5c1e1c8eef08966e21678a014a4fafb83109227c",
}


def verify_frozen() -> None:
    """Raise if any definition no longer matches its pinned hash."""
    for key, benchmark in BENCHMARKS.items():
        if DEFINITION_HASHES.get(key) != benchmark.definition_hash:
            raise RuntimeError(f"benchmark {key} was edited after it was frozen; "
                               "define a new benchmark_id instead")


def ids() -> list[str]:
    return sorted(BENCHMARKS)


def manifest() -> dict:
    verify_frozen()
    return {"benchmarks": {k: b.to_dict() for k, b in sorted(BENCHMARKS.items())},
            "metric_policy": METRIC_POLICY}


def cash(sessions) -> list[dict]:
    return [{"session": s, "benchmark_id": CASH.benchmark_id, "session_return": 0.0,
             "positions": 0} for s in sorted(sessions)]


def passive_nifty(candles: pd.DataFrame, excluded: set[str] = frozenset()) -> list[dict]:
    """Benchmark B per session. Excluded or incomplete sessions are unavailable."""
    stamps = pd.to_datetime(candles["timestamp"], utc=True).dt.tz_convert(clock_grid.IST)
    frame = candles.assign(_ist=stamps, _day=stamps.dt.date.map(lambda d: d.isoformat()),
                           _hm=stamps.dt.strftime("%H:%M"))
    rows = []
    for day, bars in frame.groupby("_day", sort=True):
        first = bars[bars["_hm"] == "09:15"]
        last = bars[bars["_hm"] == "15:25"]
        if day in excluded or first.empty or last.empty:
            rows.append({"session": day, "benchmark_id": PASSIVE_NIFTY.benchmark_id,
                         "session_return": None, "status": "unavailable",
                         "reason": "excluded_data_quality" if day in excluded
                         else "session boundary bar missing"})
            continue
        entry, exit_ = float(first["open"].iloc[0]), float(last["close"].iloc[0])
        rows.append({"session": day, "benchmark_id": PASSIVE_NIFTY.benchmark_id,
                     "entry": entry, "exit": exit_, "points": exit_ - entry,
                     "session_return": exit_ / entry - 1, "status": "ok"})
    return rows
