"""Gate, load, fingerprint, run — the order that keeps a result defensible.

Kept out of the API layer so the whole path is testable without HTTP, and
so the refusal is an exception with a report attached rather than a status
code invented at the edge.

The order is not arbitrary. Coverage is checked *before* the candles are
loaded, because a run that cannot be defended should not be run at all —
and reporting a missing-option-history problem after five minutes of walking
invites the habit of reading the numbers anyway.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from ..data import dataset as dataset_module
from ..data import repository
from ..data import research
from ..risk.manager import RiskConfig
from . import chain, strategy
from . import coverage as coverage_module
from .contracts import SelectionConfig
from ..backtest.costs import CostModel, SlippageModel
from .coverage import CoverageReport
from .pricing import MODELLED_ONLY, ModelAssumptions
from .strategy import OptionBuyConfig, OptionBuyResult

log = logging.getLogger(__name__)

# How the option archive is named in `dataset_versions`. A suffix rather
# than a separate table: the row means the same thing the index rows mean —
# a content hash of exactly what one run read — and splitting it would give
# the platform two answers to "which data produced this number?".
OPTIONS_SUFFIX = ":options"


class CoverageRefused(RuntimeError):
    """The run was refused. The report says what is missing and why."""

    def __init__(self, report: CoverageReport) -> None:
        super().__init__(report.reason or "insufficient coverage")
        self.report = report


@dataclass
class OptionBuyRequest:
    symbol: str = "NIFTY"
    timeframe: str = "5m"
    start: date | None = None
    end: date | None = None
    min_sessions: int = coverage_module.DEFAULT_MIN_SESSIONS
    min_session_coverage_pct: float = (
        coverage_module.DEFAULT_MIN_SESSION_COVERAGE_PCT)
    run: OptionBuyConfig = field(default_factory=OptionBuyConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    risk: RiskConfig | None = None
    model: ModelAssumptions = field(default_factory=ModelAssumptions)
    costs: CostModel = field(default_factory=CostModel)
    execution: SlippageModel = field(default_factory=SlippageModel)


def execute(db: Session, request: OptionBuyRequest) -> OptionBuyResult:
    """Run the option-buying backtest, or refuse with a coverage report."""
    policy = request.run.pricing_policy
    reads_archive = policy != MODELLED_ONLY

    store = (
        chain.load(db, underlying=request.symbol, timeframe=request.timeframe,
                   start=request.start, end=request.end)
        if reads_archive else chain.empty_store(request.symbol, request.timeframe)
    )

    report = coverage_module.gate(
        db, symbol=request.symbol, timeframe=request.timeframe,
        start=request.start, end=request.end, policy=policy, store=store,
        min_sessions=request.min_sessions,
        min_session_coverage_pct=request.min_session_coverage_pct)
    if not report.ok:
        raise CoverageRefused(report)

    # Grid-validated, the research read (TC-1). See `data.research`.
    candles = research.load_research_candles(
        db, request.symbol, request.timeframe,
        start=request.start, end=request.end)

    index_print = dataset_module.fingerprint(candles, request.symbol,
                                             request.timeframe)
    dataset_module.register(db, index_print)

    dataset = {"index": index_print.to_dict() | {"mode": "db"}}
    if reads_archive and not store.empty:
        option_print = store.fingerprint()
        dataset["options"] = option_print
        _register_options(db, request, store, option_print)
    else:
        dataset["options"] = {
            "consulted": False,
            "note": "No option archive was read. Every premium is modelled.",
        }

    risk = request.risk or RiskConfig(capital=request.run.starting_capital,
                                      lot_size=request.run.lot_size)

    return strategy.run(
        candles, store=store, config=request.run, risk_config=risk,
        selection=request.selection, model=request.model,
        cost_model=request.costs, slippage_model=request.execution,
        dataset=dataset, coverage=report.to_dict())


def _register_options(db: Session, request: OptionBuyRequest,
                      store: chain.ChainStore, print_: dict) -> None:
    """Record the option-archive hash beside the index one.

    Without it, two runs over identical candles can differ because the chain
    grew underneath them and both results would name the same dataset.
    """
    from datetime import UTC, datetime

    first, last = store.span()
    now = datetime.now(UTC)
    version = dataset_module.Fingerprint(
        hash=print_["hash"],
        symbol=request.symbol + OPTIONS_SUFFIX,
        timeframe=request.timeframe,
        first_ts=first.isoformat() if first else now.isoformat(),
        last_ts=last.isoformat() if last else now.isoformat(),
        row_count=print_["rows"],
        session_count=print_["sessions"],
        sources={},
        volume_is_synthetic=False,
        caveats=[],
    )
    dataset_module.register(db, version)
