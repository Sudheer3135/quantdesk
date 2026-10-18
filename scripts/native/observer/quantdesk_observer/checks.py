"""The observer's rules, written so that missing evidence can never pass.

Every check returns a record whose status is PASS, FAIL or UNKNOWN. A fact
that could not be read is UNKNOWN, and an UNKNOWN on a required check counts
against the result exactly as a FAIL does. Two further statuses carry no
judgement: RECORDED (information kept for review) and MISSING (an earlier
assessment that did not happen).

Nothing here decides anything about trading. The session checks re-read what
QuantDesk already stored — observations, linked decisions and their gate
traces — and compare it with rules Astra approved. `GATE_ORDER` restates the
order in QuantDesk's `strategy_v2/evidence.py` at the producer revision; a
trace that does not follow it is treated as inconsistent, never repaired.
"""
from __future__ import annotations

import collections
import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .probes import ProbeError

IST = ZoneInfo("Asia/Kolkata")
PASS, FAIL, UNKNOWN, RECORDED, MISSING = "PASS", "FAIL", "UNKNOWN", "RECORDED", "MISSING"

SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)
SLOT_SECONDS = 300
SESSION_SLOTS = 75
SLOT_TOLERANCE_SECONDS = 60   # Astra-approved, for matching an observation to its slot only
SCORE_TOLERANCE = 5.1e-5      # stored confidence is rounded to four decimals

GATE_ORDER = ("signal.direction", "signal.levels", "signal.age", "signal.entry_state",
              "signal.bias", "position", "kill_switch", "entry_window", "instrument_master",
              "expiry_day", "feed", "spot", "levels_vs_spot", "vix", "expiry_choice", "chain",
              "contract_selection", "lot_size", "premium_risk", "risk_manager")
DOWNSTREAM_GATES = GATE_ORDER[GATE_ORDER.index("expiry_day") + 1:]
# The rejection codes each gate can produce, restated from GATE_CODES in
# QuantDesk's strategy_v2/evidence.py and the constants in strategy_v2/rules.py
# at the frozen producer revision 2d1d25a71394. Not a contract of the
# observer's own: a decision whose failed gate and code are not paired here
# could not have been written by that producer.
GATE_CODES = {
    "signal.direction": {"no_directional_signal"},
    "signal.levels": {"signal_has_no_levels"},
    "signal.age": {"signal_too_old"},
    "signal.entry_state": {"entry_state_not_ready"},
    "signal.bias": {"bias_disagrees_with_action"},
    "position": {"position_already_open"},
    "kill_switch": {"kill_switch_engaged"},
    "entry_window": {"outside_entry_window"},
    "instrument_master": {"chain_not_ready"},
    "expiry_day": {"expiry_day"},
    "feed": {"live_feed_not_healthy"},
    "spot": {"no_live_index_price"},
    "levels_vs_spot": {"index_past_stop", "index_past_target"},
    "vix": {"vix_history_insufficient", "vix_live_unavailable", "vix_percentile_too_high",
            "vix_spiking"},
    "expiry_choice": {"no_expiry_far_enough"},
    "chain": {"chain_not_ready"},
    "contract_selection": {"chain_not_ready", "implied_volatility_unsolvable",
                           "no_strike_in_delta_band", "quote_too_old", "no_two_sided_quote",
                           "spread_too_wide", "premium_below_floor"},
    "lot_size": {"chain_not_ready"},
    "premium_risk": {"premium_risk_not_defined"},
    "risk_manager": {"risk_manager_vetoed"},
}
assert tuple(GATE_CODES) == GATE_ORDER
ENTERED = "entered"
EVIDENCE_SCHEMA = "v2_decision_evidence/2"
EVIDENCE_STATUSES = ("complete", "partial", "inconsistent", "capture_incomplete",
                     "capture_failed")
# The envelope and vector the producer writes (EvidenceBuilder.build at the
# same revision). A key listed here must be present; its value may be null,
# which is how the producer records something legitimately unavailable.
ENVELOPE_KEYS = ("schema", "status", "issues", "vector", "contract_evidence", "outcome_label",
                 "diagnostics", "bases", "missing", "missing_scope")
VECTOR_SECTIONS = {
    "identity": ("observation_id", "signal_id", "linkage", "observation_schema",
                 "seen_in_recent_history", "duplicate_check", "symbol", "timeframe",
                 "strategy", "opportunity_id", "opportunity_id_status", "strategy_family",
                 "strategy_family_status"),
    "causality": ("bar_open_time", "bar_close_time", "received_at", "available_at",
                  "available_basis", "signal_decision_at", "earliest_execution_time",
                  "execution_latency_seconds", "signal_timestamp", "signal_timestamp_basis",
                  "published_at", "generated_at", "generated_at_basis", "v2_decided_at",
                  "v2_decided_at_basis"),
    "market_state": ("signal_price", "atr14", "vwap", "trend", "india_vix_in_signal",
                     "last_structure_event", "regime_day", "regime_hour",
                     "regime_day_provisional", "regime_hour_provisional", "regime_basis",
                     "v2_spot", "v2_vix"),
    "setup": ("action", "score", "score_calibrated", "entry", "stop_loss", "target",
              "risk_reward", "bias", "bias_score", "entry_state", "trigger_level"),
    "evidence": ("checks", "threshold_used"),
    "data_status": ("disabled_checks", "signal_option_chain_present",
                    "feed_healthy_at_decision", "chain_quotes_supplied",
                    "chain_max_age_seconds"),
    "versions": ("evidence_schema", "signal_strategy_version", "parameter_hash",
                 "input_fingerprint", "input_fingerprint_note", "code_id", "code_id_basis",
                 "data_source", "v2_strategy", "v2_version", "v2_config_hash",
                 "api_init_provenance"),
    "decision_context": ("role", "outcome", "code", "gate_trace", "kill_switch",
                         "listed_expiries", "chosen_expiry", "equity", "risk", "position_id"),
}
# Issue kinds and the status each one forces (evidence.py, _ISSUE_STATUS); an
# unlisted kind forces capture_incomplete. EVIDENCE_STATUSES is in the
# producer's severity order.
TRACE_DISAGREEMENT, CAPTURE_ERROR = "selector_trace_disagreement", "capture_error"
ISSUE_STATUS = {CAPTURE_ERROR: "capture_incomplete", "gate_unreconciled": "capture_incomplete",
                TRACE_DISAGREEMENT: "inconsistent", "clock_inconsistent": "inconsistent",
                "future_predictor_excluded": "inconsistent"}
CHECK_FIELDS = ("name", "score", "weight", "contribution", "disabled", "reason")
CONTRACT_KEYS = {
    "selection_not_reached": ("status", "observation_id", "note"),
    "selection_reached": ("status", "observation_id", "expiry", "universe", "alternatives",
                          "result", "trace_consistent_with_decision", "pick", "levels"),
}
GATE_STATUS = {"passed": "PASSED", "failed": "REJECTED", "not_reached": "NOT REACHED"}
REQUIRED_JOBS = ("nifty-agent", "option-collector", "price-ticker")
JOB_COUNTERS = ("errors", "skips", "missed")
FEED_COUNTERS = ("stale_events", "login_failures", "malformed")
VIX_CLOSE_SOURCE = "angel_live"   # what QuantDesk's own close collection writes


def record(name: str, status: str, detail: str, evidence=None, required: bool = True) -> dict:
    return {"name": name, "status": status, "detail": detail,
            "evidence": evidence if evidence is not None else {}, "required": required}


def guarded(name: str, body, required: bool = True) -> dict:
    """Run one check. Anything it could not read becomes UNKNOWN.

    `RunTimeout` is a BaseException and passes straight through: the end of
    the run is not a fact about one check.
    """
    try:
        result = body()
    except ProbeError as exc:
        return record(name, UNKNOWN, str(exc), required=required)
    except Exception as exc:  # a malformed fact is unknown, not a pass
        return record(name, UNKNOWN, f"could not be evaluated: {type(exc).__name__}",
                      required=required)
    result["required"] = required
    return result


def all_pass(checks: list[dict]) -> bool:
    return all(c["status"] == PASS for c in checks if c["required"])


def failures(checks: list[dict]) -> list[str]:
    return [f"{c['name']}: {c['status']} — {c['detail']}" for c in checks
            if c["required"] and c["status"] != PASS]


def ist(stamp: str) -> datetime:
    moment = datetime.fromisoformat(stamp)
    if moment.tzinfo is None:
        raise ValueError("timestamp carries no time zone")
    return moment.astimezone(IST)


def at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=IST)


def whole(value) -> int:
    """`value` as a counter, or a ProbeError. A missing counter is never zero."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProbeError("a required counter is missing or not a number")
    return value


def finite(value) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("a numeric input is missing or not finite")
    return float(value)


# ---------------------------------------------------------------------------
# Environment checks (pre-market, and reused by the later runs)
# ---------------------------------------------------------------------------
def clock_sync(probe) -> dict:
    synced = probe.ntp_synchronized()
    return record("clock synchronization", PASS if synced else FAIL,
                  "NTP synchronized" if synced else "the clock is not NTP synchronized")


def revision(probe, cfg) -> dict:
    head, changes = probe.revision(), probe.worktree_changes()
    ok = head == cfg["expected_revision"] and not changes
    detail = ("expected revision, clean working tree" if ok else
              f"HEAD {head[:12]} (expected {cfg['expected_revision'][:12]}), "
              f"{len(changes)} working-tree change(s)")
    return record("QuantDesk revision and working tree", PASS if ok else FAIL, detail,
                  {"head": head, "worktree_changes": len(changes)})


def live_trading_off(probe) -> dict:
    live = probe.api("/health").get("live_trading")
    if live is False:
        return record("LIVE_TRADING=false", PASS, "the API reports live trading off")
    if live is True:
        return record("LIVE_TRADING=false", FAIL, "the API reports LIVE TRADING ON")
    return record("LIVE_TRADING=false", UNKNOWN, "the API did not report live_trading")


def api_pid(probe, cfg) -> int:
    pid = probe.unit(cfg["units"]["api"]).get("MainPID")
    if not str(pid).isdigit() or int(pid) == 0:
        raise ProbeError("the API unit reports no main process")
    return int(pid)


def single_runtime(probe, cfg) -> dict:
    unit = probe.unit(cfg["units"]["api"])
    matches = [pid for pid, args in probe.processes() if cfg["api_process_match"] in args]
    main_pid = int(unit.get("MainPID", "0") or 0)
    ok = unit.get("ActiveState") == "active" and matches == [main_pid]
    return record("one API/scheduler/feed runtime", PASS if ok else FAIL,
                  f"{len(matches)} API process(es); unit {unit.get('ActiveState')}",
                  {"api_pids": matches, "unit_main_pid": main_pid})


def api_healthy(probe) -> dict:
    health = probe.api("/health")
    ok = health.get("status") == "ok"
    return record("API healthy", PASS if ok else FAIL, f"status {health.get('status')!r}")


def redis_healthy(probe, cfg) -> dict:
    unit = probe.unit(cfg["units"]["redis"])
    seen = probe.api("/health").get("redis")
    ok = unit.get("ActiveState") == "active" and seen is True
    return record("Redis healthy", PASS if ok else FAIL,
                  f"unit {unit.get('ActiveState')}; API sees redis: {seen}")


def postgres_healthy(probe, cfg) -> dict:
    unit = probe.unit(cfg["units"]["postgres"])
    probe.journal()  # one read proves the database answers a query
    ok = unit.get("ActiveState") == "active"
    return record("PostgreSQL healthy", PASS if ok else FAIL,
                  f"unit {unit.get('ActiveState')}; a read-only query answered")


def scheduler_jobs(probe) -> list[dict]:
    """One record per scheduler job, from the API's own verdict on itself.

    Lifetime counters are recorded, not judged: no "zero errors ever" rule
    exists. A job that is absent, or whose counters are absent, is UNKNOWN.
    """
    labels = dict(zip(REQUIRED_JOBS, ("signal agent healthy", "option collector healthy",
                                      "price ticker healthy"), strict=True))
    try:
        health = probe.api("/health/scheduler")
    except ProbeError as exc:
        return [record(label, UNKNOWN, str(exc)) for label in labels.values()]
    out = []
    for job_id, label in labels.items():
        job = (health.get("jobs") or {}).get(job_id)
        if not isinstance(job, dict):
            out.append(record(label, UNKNOWN, f"job {job_id} is not reported by the scheduler"))
            continue
        try:
            counters = {k: whole(job.get(k)) for k in JOB_COUNTERS}
        except ProbeError as exc:
            out.append(record(label, UNKNOWN, f"job {job_id}: {exc}"))
            continue
        if not isinstance(health.get("healthy"), bool) or not isinstance(job.get("starved"), bool):
            out.append(record(label, UNKNOWN, "the scheduler did not report its health"))
            continue
        ok = health["healthy"] and not job["starved"] and not health.get("problems")
        out.append(record(label, PASS if ok else FAIL,
                          "the scheduler reports the job healthy" if ok else
                          f"scheduler healthy={health['healthy']}, starved={job['starved']}, "
                          f"problems={health.get('problems')}",
                          {"lifetime_counters": counters,
                           "counting_since": health.get("counting_since")}))
    return out


def angel_premarket(probe) -> dict:
    """Initialized for a market that has not opened: a session, no ticks required."""
    angel = probe.api("/health/feed").get("angel") or {}
    session, counters = angel.get("session") or {}, angel.get("counters") or {}
    failed_logins = whole(counters.get("login_failures"))
    ok = (angel.get("enabled") is True and session.get("has_auth_token") is True
          and session.get("has_feed_token") is True and whole(counters.get("logins")) >= 1)
    return record("Angel initialized for pre-market", PASS if ok else FAIL,
                  "a session is established; live ticks are not required before the open" if ok
                  else "no established Angel session",
                  {"state": angel.get("state"), "logins": counters.get("logins"),
                   "login_failures": failed_logins})


def angel_in_session(probe) -> dict:
    feed = probe.api("/health/feed")
    angel, chain = feed.get("angel") or {}, feed.get("option_chain") or {}
    if not all(isinstance(v, bool) for v in (angel.get("healthy"), chain.get("ready"))):
        raise ProbeError("the API did not report feed health")
    ok = angel.get("state") == "live" and angel["healthy"] and chain["ready"]
    return record("Angel feed live", PASS if ok else FAIL,
                  f"state {angel.get('state')!r}, healthy {angel['healthy']}, "
                  f"option stream ready {chain['ready']}")


def maintenance_review(probe, cfg, now: datetime) -> dict:
    """Boot, service starts and unattended upgrades, kept for review.

    PASS means the evidence was read, not that nothing happened. A restart is
    recorded with the times systemd reports; `NRestarts` counts automatic
    restarts only and does not prove a complete restart history.
    """
    boot = datetime.fromtimestamp(probe.boot_time_epoch(), IST)
    units = {key: probe.unit(name) for key, name in cfg["units"].items()}
    lines = probe.log_lines(cfg["upgrade_log"], (now.strftime("%Y-%m-%d"),
                            (now - timedelta(days=1)).strftime("%Y-%m-%d")))
    return record("overnight update/reboot/service-start review", PASS,
                  "boot time, unit start times and the upgrade log were read and recorded",
                  {"boot_ist": boot.isoformat(),
                   "units": {k: {"active_since": u.get("ActiveEnterTimestamp"),
                                 "main_process_started": u.get("ExecMainStartTimestamp"),
                                 "automatic_restarts": u.get("NRestarts")}
                             for k, u in units.items()},
                   "upgrade_log": lines,
                   "note": "NRestarts alone does not prove a complete restart history"})


def pending_restart(probe) -> dict:
    """Maintenance information. A pending reboot is recorded, never acted on."""
    needed = probe.reboot_required()
    return record("pending restart requirements", RECORDED,
                  "a reboot is pending (maintenance information; not a failure by itself)"
                  if needed else "no reboot pending", {"reboot_required": needed},
                  required=False)


def positions_unchanged(probe, baseline: dict | None, day: date) -> dict:
    """Before the open: stored paper activity must equal the trusted baseline."""
    stored, journal = probe.positions(day), probe.journal()
    engine = probe.api("/v2/status")
    if "position" not in engine:
        raise ProbeError("the paper engine did not report a position field")
    now = {"positions_total": whole(stored.get("total")),
           "positions_open": whole(stored.get("open")),
           "journal_total": whole(journal.get("total"))}
    name = "paper position reconciliation; no unexplained order/fill"
    if not baseline or any(baseline.get(k) is None for k in ("positions_total", "journal_total")):
        return record(name, UNKNOWN, "no trusted baseline to compare paper activity against", now)
    problems = []
    engine_open = 0 if engine["position"] is None else 1
    if now["positions_open"] != engine_open:
        problems.append(f"{now['positions_open']} open stored, engine holds {engine_open}")
    for key in ("positions_total", "journal_total"):
        if baseline[key] != now[key]:
            problems.append(f"{key} was {baseline[key]} at the baseline, now {now[key]}")
    return record(name, FAIL if problems else PASS,
                  "; ".join(problems) or "stored paper activity matches the engine and the "
                  "trusted baseline", {**now, "baseline": baseline})


def disk_space(probe, cfg) -> dict:
    low, seen = [], {}
    for path in cfg["disk_paths"]:
        free, total = probe.disk_free(path)
        seen[path] = {"free_bytes": free, "total_bytes": total}
        if free < cfg["disk_min_free_bytes"] or free / total < cfg["disk_min_free_fraction"]:
            low.append(path)
    return record("adequate disk space", FAIL if low else PASS,
                  f"low on {low}" if low else "free space above the configured floor", seen)


def deployment_unchanged(probe, cfg) -> dict:
    listeners = probe.listeners()
    extra = sorted(set(listeners) - set(cfg["expected_listeners"]))
    gone = sorted(set(cfg["expected_listeners"]) - set(listeners))
    caddy = probe.program_present("caddy")
    problems = ([f"unexpected listeners {extra}"] if extra else []) \
        + ([f"missing listeners {gone}"] if gone else []) \
        + (["Caddy is installed"] if caddy and not cfg.get("https_deployed", False) else [])
    return record("no unexpected deployment/network changes", FAIL if problems else PASS,
                  "; ".join(problems) or "listening sockets match the recorded set; "
                  "Azure network rules and DNS are not observable from the VM",
                  {"listeners": listeners})


def counter_snapshot(probe, cfg, now: datetime) -> dict:
    """The API's cumulative counters with the identity of the process they belong to.

    Kept so a later run can take a difference. Every required field must be
    present: an absent job or counter makes the snapshot unusable, not zero.
    """
    sched, feed = probe.api("/health/scheduler"), probe.api("/health/feed")
    angel = feed.get("angel") or {}
    counters, jobs = angel.get("counters") or {}, sched.get("jobs") or {}
    identity = {"api_pid": api_pid(probe, cfg), "counting_since": sched.get("counting_since"),
                "feed_started_at": angel.get("started_at")}
    if not all(identity.values()):
        raise ProbeError("the API did not report the identity of its counters")
    missing = [job for job in REQUIRED_JOBS if not isinstance(jobs.get(job), dict)]
    if missing:
        raise ProbeError(f"scheduler jobs not reported: {', '.join(missing)}")
    snapshot = {"taken_at_ist": now.astimezone(IST).isoformat(), "identity": identity,
                "jobs": {job: {k: whole(jobs[job].get(k)) for k in JOB_COUNTERS}
                         for job in REQUIRED_JOBS},
                "feed": {k: whole(counters.get(k)) for k in FEED_COUNTERS},
                "reconnects_recorded_not_judged": counters.get("reconnects")}
    return record("API counter snapshot", RECORDED, "kept for a later difference", snapshot,
                  required=False)


MAC_PREREQUISITE = {
    "name": "Mac QuantDesk runtime stopped", "status": "OPERATOR-MAINTAINED PREREQUISITE",
    "required": False, "evidence": {},
    "detail": "the Azure observer cannot see the Mac; this is not evaluated"}


def premarket_checks(probe, cfg, baseline: dict | None, now: datetime) -> list[dict]:
    day = now.date()
    return [
        guarded("clock synchronization", lambda: clock_sync(probe)),
        guarded("QuantDesk revision and working tree", lambda: revision(probe, cfg)),
        guarded("LIVE_TRADING=false", lambda: live_trading_off(probe)),
        guarded("one API/scheduler/feed runtime", lambda: single_runtime(probe, cfg)),
        guarded("PostgreSQL healthy", lambda: postgres_healthy(probe, cfg)),
        guarded("Redis healthy", lambda: redis_healthy(probe, cfg)),
        guarded("API healthy", lambda: api_healthy(probe)),
        *scheduler_jobs(probe),
        guarded("Angel initialized for pre-market", lambda: angel_premarket(probe)),
        guarded("overnight update/reboot/service-start review",
                lambda: maintenance_review(probe, cfg, now)),
        guarded("paper position reconciliation; no unexplained order/fill",
                lambda: positions_unchanged(probe, baseline, day)),
        guarded("adequate disk space", lambda: disk_space(probe, cfg)),
        guarded("no unexpected deployment/network changes",
                lambda: deployment_unchanged(probe, cfg)),
        guarded("pending restart requirements", lambda: pending_restart(probe), required=False),
        guarded("API counter snapshot", lambda: counter_snapshot(probe, cfg, now),
                required=False),
        dict(MAC_PREREQUISITE),
    ]


# ---------------------------------------------------------------------------
# Session evidence
# ---------------------------------------------------------------------------
def _interval(seconds: float) -> str:
    return f"interval[{timedelta(seconds=seconds)}]"


AGENT_TRIGGER = _interval(SLOT_SECONDS)


def agent_identity_problems(segment: dict) -> list[str]:
    """Why one scheduler start's log does not single out the signal agent's runs.

    APScheduler's run lines carry a function name and a trigger, never the
    job id `nifty-agent`, and all three QuantDesk jobs run a function called
    `tick`. A five-minute `tick` line is therefore the agent's only if the
    same scheduler start shows all of: the agent's own declaration of a
    five-minute schedule (`app.workers.agent`), the ticker's and collector's
    own declarations of different intervals, exactly the jobs the frozen
    application registers, and no run by any other job or trigger. A
    matching interval alone establishes nothing.
    """
    problems = []
    if not (segment.get("start_logged") and segment.get("scheduler_started")):
        problems.append("the log does not retain this scheduler's start, so what it "
                        "registered and declared is missing")
    if segment.get("runs_not_kept"):
        problems.append("more run lines than the observer keeps")

    def declared(key: str, what: str, optional: bool = False) -> float | None:
        values = segment.get(key) or []
        if optional and not values and segment.get("collector_disabled"):
            return None
        if len(values) != 1:
            problems.append(f"{what} declared its interval {len(values)} times")
            return None
        try:
            return finite(float(values[0]))
        except (TypeError, ValueError):
            problems.append(f"{what} declared an unreadable interval")
            return None

    minutes = declared("agent_minutes", "the signal agent (`Nifty agent scheduled`)")
    ticker = declared("ticker_seconds", "the price ticker")
    collector = declared("collector_seconds", "the option collector", optional=True)
    if minutes is not None and minutes * 60 != SLOT_SECONDS:
        problems.append(f"the signal agent declared a run every {minutes:g} minutes; the "
                        "five-minute session template does not apply")
    others = {_interval(v) for v in (ticker, collector) if v is not None}
    if AGENT_TRIGGER in others:
        problems.append("another QuantDesk job declares the same five-minute interval")
    jobs = 2 + (not segment.get("collector_disabled"))
    if segment.get("added") != ["tick"] * jobs:
        problems.append(f"the scheduler registered {segment.get('added')!r}, not the "
                        f"{jobs} jobs the frozen application defines")
    strangers = [pair for pair in segment.get("jobs_seen") or []
                 if pair[0] != "tick" or pair[1] not in others | {AGENT_TRIGGER}]
    if strangers:
        problems.append(f"runs by a job the frozen application does not define: {strangers[:3]}")
    return problems


def schedule_basis(logged: list[dict] | None, day: date, until: datetime | None = None) -> dict:
    """The signal agent's schedule for the session, or why it is UNKNOWN.

    `logged` is `probes.parse_scheduler_log`: one entry per scheduler start.
    The schedule is ESTABLISHED only when every start that ran anything on
    the session date identifies the agent's runs (`agent_identity_problems`),
    those runs sit on one five-minute grid, and every slot due by `until`
    (the whole session when None) is itself a logged scheduled time. It is
    never taken from the API's start time, another interval job, the market
    open or the observations, and a slot the log does not hold is not
    filled in from its neighbours. The one start that ran on the session
    date must also be the running API process's own (`attribution` status
    `current`, see `probes.attribute_startups`). A start that is
    `within_start_second`, `earlier`, `later`, `ambiguous` or
    `unattributed` establishes nothing.
    """
    opened, closed = at(day, SESSION_OPEN), at(day, SESSION_CLOSE)
    if logged is None:
        return {"status": UNKNOWN, "reason": "the scheduler log could not be read"}
    if not isinstance(logged, list) or not all(isinstance(s, dict) for s in logged):
        return {"status": UNKNOWN, "reason": "the scheduler log evidence is unreadable"}
    scope = [s for s in logged if s.get("runs")]
    if not scope:
        return {"status": UNKNOWN, "reason": "the scheduler logged no run time on the "
                                             "session date"}
    problems = sorted({p for s in scope for p in agent_identity_problems(s)})
    if problems:
        return {"status": UNKNOWN, "scheduler_starts": len(scope),
                "identity_problems": problems,
                "reason": "the signal agent's job cannot be identified in the scheduler "
                          "log: " + "; ".join(problems)}
    try:
        times = sorted(ist(stamp) for s in scope for name, trigger, stamp in s["runs"]
                       if trigger == AGENT_TRIGGER)
    except (TypeError, ValueError):
        return {"status": UNKNOWN, "reason": "the scheduler log holds an unreadable run time"}
    times = [t for t in times if t.date() == day]
    if not times:
        return {"status": UNKNOWN, "reason": "the scheduler logged no run time for the "
                                             "signal agent on the session date"}
    step = timedelta(seconds=SLOT_SECONDS)
    phases = {(t - opened) % step for t in times}
    if len(phases) != 1 or len(scope) != 1:
        return {"status": UNKNOWN, "logged_run_times": len(times), "scheduler_starts": len(scope),
                "reason": f"the logged run times come from {len(scope)} scheduler start(s) on "
                          f"{len(phases)} different five-minute grid(s); the timing evidence "
                          "conflicts and no single schedule is established"}
    attribution = scope[0].get("attribution") or {}
    if attribution.get("status") != "current":
        return {"status": UNKNOWN, "logged_run_times": len(times), "scheduler_starts": 1,
                "attribution": attribution or None,
                "reason": "the scheduler start that logged the session's runs is not the "
                          "running API process's own start ("
                          f"{attribution.get('status', 'not attributed')}); another start's "
                          "evidence does not establish the schedule"}
    first = opened + phases.pop()
    slots = [first + i * step for i in range(SESSION_SLOTS)]
    slots = [slot for slot in slots if slot < closed]
    due = [s for s in slots if until is None
           or s + timedelta(seconds=SLOT_TOLERANCE_SECONDS) <= until]
    unlogged = [s for s in due if s not in times]
    found = {"logged_run_times": len(times), "scheduler_starts": 1,
             "first_logged_ist": times[0].isoformat(), "last_logged_ist": times[-1].isoformat(),
             "logged_before_the_open": times[0] < opened,
             "slots_the_scheduler_did_not_log": [s.strftime("%H:%M:%S") for s in unlogged][:20],
             "startup": {**(scope[0].get("startup") or {}), **attribution}}
    if "capture" in scope[0]:
        # v0.4.12: whether the start was captured now, or read from a kept capture.
        found["startup_capture"] = scope[0]["capture"]
    if unlogged:
        return {"status": UNKNOWN, **found,
                "reason": f"{len(unlogged)} scheduled slot(s) due by now are not in the "
                          "retained log (a gap, lost lines or a skipped run); they are not "
                          "filled in from the others"}
    return {"status": "ESTABLISHED", "slots": slots, **found,
            "basis": "five-minute `tick` run lines logged by QuantDesk's scheduler, attributed "
                     "to the signal agent by its own start-up declaration and by the absence "
                     "of any other five-minute job on that scheduler",
            "identity_note": "the run lines do not carry the job id nifty-agent; the "
                             "attribution rests on the declarations logged at that start"}


def collection(signals: list[dict], schedule: dict, day: date, now: datetime,
               final: bool) -> dict:
    """Coverage of the predeclared schedule (see `schedule_basis`).

    The 60-second tolerance matches one observation to one scheduled slot and
    does nothing else: it does not move the schedule, merge two observations,
    change a timestamp, or excuse a slot with no observation.
    """
    name = "collection coverage"
    opened, closed = at(day, SESSION_OPEN), at(day, SESSION_CLOSE)
    if schedule["status"] != "ESTABLISHED":
        return record(name, UNKNOWN, "the observation schedule cannot be established: "
                      + schedule["reason"], {"actual": len(signals), "schedule": schedule})
    slots = schedule["slots"]
    due = [slot for slot in slots
           if final or slot + timedelta(seconds=SLOT_TOLERANCE_SECONDS) <= now]
    matched, deviations, off_schedule, outside = collections.Counter(), [], [], []
    for s in signals:
        made = ist(s["created_at"])
        if not opened <= made < closed + timedelta(seconds=SLOT_TOLERANCE_SECONDS):
            outside.append(s["id"])
            continue
        nearest = min(slots, key=lambda slot: abs((made - slot).total_seconds()))
        deviation = (made - nearest).total_seconds()
        if abs(deviation) > SLOT_TOLERANCE_SECONDS:
            off_schedule.append(s["id"])
        else:
            matched[nearest] += 1
            deviations.append(deviation)
    missing_slots = [slot for slot in due if slot not in matched]
    bars = collections.Counter(s.get("bar_open_time") for s in signals)
    duplicates = max(sum(n - 1 for n in matched.values()), sum(n - 1 for n in bars.values()))
    ids = [s["id"] for s in signals]
    evidence = {"expected": len(due), "actual": len(signals), "matched_slots": len(matched),
                "missing": len(missing_slots),
                "missing_slots_ist": [m.strftime("%H:%M:%S") for m in missing_slots[:20]],
                "duplicates": duplicates, "off_schedule": off_schedule[:20],
                "outside_window": outside[:20],
                "signal_ids": [min(ids), max(ids)] if ids else None,
                "seconds_after_slot": ({"min": min(deviations), "max": max(deviations)}
                                       if deviations else None),
                "schedule": {k: v for k, v in schedule.items() if k != "slots"}}
    ok = (not missing_slots and not duplicates and not off_schedule and not outside
          and (not final or len(matched) == SESSION_SLOTS == len(signals) == len(slots)))
    return record(name, PASS if ok else FAIL,
                  f"{len(matched)} of {len(due)} scheduled observations; "
                  f"{len(missing_slots)} missing, {duplicates} duplicate, "
                  f"{len(off_schedule)} off schedule, {len(outside)} outside the session",
                  evidence)


def _evidence(decision: dict) -> dict:
    return decision.get("evidence") or {}


def _vector(decision: dict) -> dict:
    return _evidence(decision).get("vector") or {}


def producer_ok(signal: dict, cfg) -> bool:
    repo = (signal.get("api_init") or {}).get("repo_state") or {}
    return (signal.get("code_id") == cfg["expected_revision"][:12]
            and repo.get("git_commit") == cfg["expected_revision"]
            and repo.get("dirty_worktree") is False)


def producer(signals: list[dict], cfg) -> dict:
    bad = [s["id"] for s in signals if not producer_ok(s, cfg)]
    ids = [s.get("observation_id") for s in signals]
    unique = all(ids) and len(set(ids)) == len(ids)
    ok = signals and not bad and unique
    return record("producer revision", PASS if ok else FAIL,
                  f"{len(signals) - len(bad)} of {len(signals)} from the expected clean revision; "
                  f"observation ids {'unique' if unique else 'missing or repeated'}",
                  {"code_ids": sorted({str(s.get('code_id')) for s in signals}),
                   "not_expected": bad[:20]})


def runtime_continuity(signals: list[dict]) -> dict:
    """One API initialization across the session, from stored provenance.

    A restart is recorded with its times and sent for review; it is not by
    itself a collection failure.
    """
    name = "single runtime during collection"
    runs = sorted({((s.get("api_init") or {}).get("pid"),
                    (s.get("api_init") or {}).get("initialized_at")) for s in signals}, key=str)
    if not signals or not all(pid is not None and stamp for pid, stamp in runs):
        return record(name, UNKNOWN, "observations carry no API initialization provenance")
    evidence = {"runtimes": [{"pid": p, "initialized_at": t} for p, t in runs],
                "restarts_during_collection": len(runs) - 1}
    if len(runs) == 1:
        return record(name, PASS, "one API initialization produced every observation", evidence)
    return record(name, FAIL, f"{len(runs)} API initializations produced the observations; "
                  "the restart and its effect on continuity need review", evidence)


def config_ok(signal: dict, decision: dict, cfg) -> bool:
    versions = _vector(decision).get("versions") or {}
    return (signal.get("parameter_hash") == cfg["expected_parameter_hash"]
            and signal.get("strategy_version") == cfg["expected_strategy_version"]
            and versions.get("v2_config_hash") == cfg["expected_v2_config_hash"])


def configuration(signals: list[dict], decisions: list[dict], cfg) -> dict:
    hashes = {s.get("parameter_hash") for s in signals}
    versions = {s.get("strategy_version") for s in signals}
    v2 = {(_vector(d).get("versions") or {}).get("v2_config_hash") for d in decisions}
    ok = (hashes == {cfg["expected_parameter_hash"]} and v2 == {cfg["expected_v2_config_hash"]}
          and versions == {cfg["expected_strategy_version"]})
    return record("configuration identity", PASS if ok else FAIL,
                  "parameter hash, strategy version and v2 configuration unchanged" if ok
                  else "configuration identity differs from the accepted baseline",
                  {"parameter_hashes": sorted(map(str, hashes)),
                   "v2_config_hashes": sorted(map(str, v2)),
                   "strategy_versions": sorted(map(str, versions))})


def evidence_problems(decision: dict) -> dict:
    """What is wrong with a decision's stored evidence, by kind.

    Checked against the envelope QuantDesk's EvidenceBuilder writes at the
    frozen producer revision — not a shape of the observer's choosing.

      missing        a required section or field is absent (C)
      contradictory  the record disagrees with itself, or is labelled
                     complete when it is not (D)

    Two things are not problems: a required field that is present and null
    (the producer's way of recording something legitimately unavailable, A),
    and `contract_evidence.status = selection_not_reached` on a decision that
    stopped before the selector (B).
    """
    evidence = decision.get("evidence")
    if not isinstance(evidence, dict):
        return {"missing": ["evidence"], "contradictory": []}
    missing = [k for k in ENVELOPE_KEYS if k not in evidence]
    contradictory = []
    vector = evidence.get("vector") if isinstance(evidence.get("vector"), dict) else {}
    for section, keys in VECTOR_SECTIONS.items():
        body = vector.get(section)
        if not isinstance(body, dict):
            missing.append(f"vector.{section}")
            continue
        missing += [f"vector.{section}.{k}" for k in keys if k not in body]
    checks_ = (vector.get("evidence") or {}).get("checks")
    if not isinstance(checks_, list) or not all(
            isinstance(c, dict) and all(f in c for f in CHECK_FIELDS) for c in checks_):
        missing.append("vector.evidence.checks[*] fields")
    contract = evidence.get("contract_evidence")
    status = contract.get("status") if isinstance(contract, dict) else None
    if status not in CONTRACT_KEYS:
        if "contract_evidence" in evidence:
            contradictory.append(f"contract_evidence.status is {status!r}")
    else:
        missing += [f"contract_evidence.{k}" for k in CONTRACT_KEYS[status] if k not in contract]
        if status == "selection_reached" and not isinstance(contract.get("universe"), dict):
            missing.append("contract_evidence.universe")
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        contradictory.append(f"schema is {evidence.get('schema')!r}")
    if evidence.get("status") not in EVIDENCE_STATUSES:
        contradictory.append(f"status is {evidence.get('status')!r}")
    if not isinstance(evidence.get("issues", []), list):
        contradictory.append("issues is not a list")
    identity = vector.get("identity") if isinstance(vector.get("identity"), dict) else {}
    versions = vector.get("versions") if isinstance(vector.get("versions"), dict) else {}
    context = vector.get("decision_context") \
        if isinstance(vector.get("decision_context"), dict) else {}
    setup = vector.get("setup") if isinstance(vector.get("setup"), dict) else {}
    if evidence.get("status") == "complete":
        if evidence.get("issues"):
            contradictory.append("labelled complete while listing issues")
        if identity.get("linkage") != "linked":
            contradictory.append("labelled complete without a linked observation")
        if missing:
            contradictory.append("labelled complete with required evidence missing")
    contradictory += selector_contradictions(evidence, contract if status else None)
    if "evidence_schema" in versions and versions["evidence_schema"] != evidence.get("schema"):
        contradictory.append("versions.evidence_schema differs from the envelope's schema")
    for key in ("outcome", "code"):
        if key in context and context[key] != decision.get(key):
            contradictory.append(f"decision_context.{key} differs from the decision's {key}")
    if "action" in setup and setup["action"] != decision.get("action"):
        contradictory.append("setup.action differs from the decision's action")
    trace = context.get("gate_trace")
    selector = next((g.get("status") for g in trace if isinstance(g, dict)
                     and g.get("gate") == "contract_selection"), None) \
        if isinstance(trace, list) else None
    if status == "selection_not_reached" and selector in ("passed", "failed"):
        contradictory.append("contract evidence says selection was not reached, but the "
                             "trace reaches the selector")
    if status == "selection_reached" and selector not in ("passed", "failed"):
        contradictory.append("contract evidence says selection was reached, but the trace "
                             "does not reach the selector")
    return {"missing": missing, "contradictory": contradictory}


def selector_contradictions(evidence: dict, contract: dict | None) -> list[str]:
    """Where the stored selector evidence and the record's own status disagree.

    Restated from EvidenceBuilder.build and status_for at the frozen producer
    revision. When the re-run selector disagrees with the decision the
    producer stores trace_consistent_with_decision = false AND files a
    `selector_trace_disagreement` issue, which makes the status `inconsistent`
    at best. When the trace was not captured it stores null AND files a
    capture error, which makes the status `capture_incomplete`. So:

      false          always a contradiction in the evidence itself: the
                     selection on record is not the selection that decided
      null           legitimate only beside the capture error that explains
                     it; never under a status of `complete`
      true           legitimate
      not reached    there is no selector evidence, and nothing to contradict

    The envelope's status must also be the one the producer's rule gives for
    the issues it lists.
    """
    problems = []
    issues = evidence.get("issues")
    issues = [i for i in issues if isinstance(i, dict)] if isinstance(issues, list) else []
    kinds = [i.get("kind") for i in issues]
    reached = isinstance(contract, dict) and contract.get("status") == "selection_reached"
    consistent = contract.get("trace_consistent_with_decision") if reached else None
    if reached and "trace_consistent_with_decision" in contract:
        if consistent is False:
            problems.append("the selector trace disagrees with the decision "
                            f"({TRACE_DISAGREEMENT})")
            if TRACE_DISAGREEMENT not in kinds:
                problems.append("the trace disagreement is not filed among the issues")
        elif consistent is None:
            if CAPTURE_ERROR not in kinds:
                problems.append("the selector trace is absent with no capture error to "
                                "explain it")
        elif consistent is not True:
            problems.append(f"trace_consistent_with_decision is {consistent!r}")
    if TRACE_DISAGREEMENT in kinds and consistent is not False:
        problems.append("a trace disagreement is filed, but the contract evidence does not "
                        "record one")
    vector = evidence.get("vector") if isinstance(evidence.get("vector"), dict) else {}
    identity = vector.get("identity") if isinstance(vector.get("identity"), dict) else {}
    if evidence.get("status") in EVIDENCE_STATUSES and "linkage" in identity \
            and isinstance(evidence.get("issues"), list):
        expected = "complete" if identity["linkage"] == "linked" else "partial"
        for kind in kinds:
            implied = ISSUE_STATUS.get(kind, "capture_incomplete")
            if EVIDENCE_STATUSES.index(implied) > EVIDENCE_STATUSES.index(expected):
                expected = implied
        if evidence["status"] != expected:
            problems.append(f"status is {evidence['status']!r} where the producer's rule "
                            f"gives {expected!r} for the issues and linkage on record")
    return problems


def decision_is_sound(decision: dict, signal_by_id: dict) -> bool:
    """Structurally complete v2_decision_evidence/2, labelled complete, and linked
    to one of this session's observations with matching identity."""
    found = evidence_problems(decision)
    if found["missing"] or found["contradictory"]:
        return False
    evidence, vector = _evidence(decision), _vector(decision)
    identity, versions = vector["identity"], vector["versions"]
    signal = signal_by_id.get(identity.get("signal_id"))
    return (evidence["status"] == "complete" and signal is not None
            and identity.get("observation_schema") == "signal_observation/2"
            and signal.get("observation_id") == identity.get("observation_id")
            and signal.get("action") == decision.get("action")
            and versions.get("code_id") == signal.get("code_id")
            and versions.get("parameter_hash") == signal.get("parameter_hash"))


def linkage(signals: list[dict], decisions: list[dict]) -> dict:
    by_id = {s["id"]: s for s in signals}
    sound = [d for d in decisions if decision_is_sound(d, by_id)]
    per_signal = collections.Counter((_vector(d).get("identity") or {}).get("signal_id")
                                     for d in sound)
    orphans = [s["id"] for s in signals if per_signal.get(s["id"], 0) != 1]
    kinds = {"missing_required_evidence": {}, "contradictory_or_mislabelled": {}}
    for d in decisions:
        found = evidence_problems(d)
        if found["missing"]:
            kinds["missing_required_evidence"][d.get("id")] = found["missing"][:8]
        if found["contradictory"]:
            kinds["contradictory_or_mislabelled"][d.get("id")] = found["contradictory"][:8]
    ok = signals and len(sound) == len(decisions) == len(signals) and not orphans
    return record("observation/decision linkage and evidence status", PASS if ok else FAIL,
                  f"{len(sound)} of {len(decisions)} decisions carry complete, structurally "
                  f"valid, linked evidence; {len(orphans)} observation(s) without exactly one",
                  {"decisions": len(decisions), "sound": len(sound),
                   "observations_unlinked": orphans[:20],
                   **{k: dict(list(v.items())[:20]) for k, v in kinds.items()}})


def trace_problems(decision: dict) -> list[str]:
    """Why a stored gate trace cannot be taken at its word. Empty means it can.

    The trace must name every gate once, in QuantDesk's order; pass (or be
    disabled) up to at most one failure; reach nothing after it; and agree
    with the decision's own outcome and rejection code.
    """
    context = _vector(decision).get("decision_context") or {}
    trace = context.get("gate_trace")
    if not isinstance(trace, list) or not all(isinstance(g, dict) for g in trace):
        return ["the decision carries no gate trace"]
    names = [g.get("gate") for g in trace]
    if names != list(GATE_ORDER):
        repeated = sorted({str(n) for n in names if names.count(n) > 1})
        return [f"gates repeated: {repeated}" if repeated
                else "the trace does not list every gate once in the expected order"]
    problems, failed, stopped = [], [], False
    for gate in trace:
        name, status, enabled = gate["gate"], gate.get("status"), gate.get("enabled")
        if not isinstance(enabled, bool) or status not in ("passed", "failed", "not_reached",
                                                           "disabled"):
            problems.append(f"{name}: enabled={enabled!r}, status={status!r}")
            continue
        if status != "not_reached" and (status == "disabled") == enabled:
            problems.append(f"{name}: status {status} contradicts enabled={enabled}")
        if stopped and status != "not_reached":
            problems.append(f"{name}: {status} after the decision had already stopped")
        if status == "failed":
            failed.append(gate)
        if status in ("failed", "not_reached"):
            stopped = True
    outcome, code = decision.get("outcome"), decision.get("code")
    if context.get("outcome") not in (None, outcome) or context.get("code") not in (None, code):
        problems.append("the trace's outcome or code differs from the decision's")
    if any("code" in g for g in trace if g.get("status") != "failed"):
        problems.append("a gate that did not fail carries a rejection code")
    if outcome == "rejected":
        if len(failed) != 1:
            problems.append(f"a rejection with {len(failed)} failed gate(s)")
        else:
            gate = failed[0]["gate"]
            if failed[0].get("code") != code:
                problems.append(f"rejected as {code!r} but the failed gate {gate} records "
                                f"{failed[0].get('code')!r}")
            if code not in GATE_CODES[gate]:
                problems.append(f"{code!r} is not a code the {gate} gate produces; the "
                                "decision and its trace disagree about where it stopped")
    elif outcome == ENTERED:
        if failed or stopped:
            problems.append("an entry whose trace does not pass every gate")
        if code != ENTERED:
            problems.append(f"an entry recorded with code {code!r}")
        if context.get("position_id") is None:
            problems.append("an entry with no position id")
    else:
        problems.append(f"unrecognized decision outcome {outcome!r}")
    return problems


def gate_traces(decisions: list[dict]) -> dict:
    bad = {d["id"]: trace_problems(d) for d in decisions}
    bad = {k: v for k, v in bad.items() if v}
    return record("gate traces internally consistent", FAIL if bad or not decisions else PASS,
                  f"{len(bad)} of {len(decisions)} decisions carry a trace that contradicts "
                  "itself or the decision", {"inconsistent": dict(list(bad.items())[:20])})


def gate_map(decision: dict) -> dict:
    trace = (_vector(decision).get("decision_context") or {}).get("gate_trace") or []
    return {g.get("gate"): g for g in trace if isinstance(g, dict)}


def claims_expiry_day_pass(decision: dict) -> bool:
    trace = (_vector(decision).get("decision_context") or {}).get("gate_trace") or []
    return isinstance(trace, list) and any(
        isinstance(g, dict) and g.get("gate") == "expiry_day" and g.get("status") == "passed"
        for g in trace)


def expiry_day_passed(decision: dict) -> bool:
    """A consistent trace in which expiry_day is enabled and passed. Nothing looser."""
    if trace_problems(decision):
        return False
    gate = gate_map(decision)["expiry_day"]
    return gate["enabled"] is True and gate["status"] == "passed"


def _check(signal: dict, name: str) -> dict | None:
    return next((c for c in signal.get("checks") or [] if c.get("name") == name), None)


def option_chain(signals: list[dict], cfg) -> dict:
    """Astra's rule for this phase: enabled with valid available OI on every observation.

    A chain that was genuinely unavailable is a valid recorded condition and
    is listed as such; it still does not satisfy this gate.
    """
    failed, counts = [], collections.Counter()
    for s in signals:
        chain = _check(s, "option_chain")
        reasons = []
        if chain is None:
            reasons.append("no option-chain check stored")
        else:
            if chain.get("disabled") is not False:
                reasons.append("check not enabled")
            if chain.get("weight") != cfg["expected_option_chain_weight"]:
                reasons.append(f"weight {chain.get('weight')}")
        if s.get("oi_status") != "available":
            reasons.append(f"OI {s.get('oi_status')}")
        if reasons:
            failed.append({"signal_id": s["id"], "failed_requirement": "; ".join(reasons),
                           "stored_reason": (chain or {}).get("reason")})
            continue
        value = chain.get("contribution") or 0
        counts["positive" if value > 0 else "negative" if value < 0 else "neutral"] += 1
    ok = signals and not failed
    return record("option chain enabled with available OI on every observation",
                  PASS if ok else FAIL,
                  f"{len(signals) - len(failed)} of {len(signals)} observations satisfy it",
                  {"satisfied": len(signals) - len(failed), "not_satisfied": failed[:75],
                   **{k: counts[k] for k in ("positive", "negative", "neutral")}})


def score_accounting(signals: list[dict], decisions: list[dict]) -> dict:
    """Recompute QuantDesk's own score from its stored inputs; change nothing.

    Confidence is |Σ score×weight ÷ Σ enabled weight|. Below the stored
    threshold the action is HOLD; otherwise BUY when the signed score is
    positive and SELL when it is negative.
    """
    worst, problems = 0.0, {}
    setup = {(_vector(d).get("identity") or {}).get("signal_id"):
             (_vector(d).get("setup") or {}).get("score") for d in decisions}
    for s in signals:
        try:
            checks = s["checks"]
            for c in checks:
                finite(c["score"]), finite(c["weight"]), finite(c["contribution"])
                if not isinstance(c["disabled"], bool):
                    raise ValueError("disabled is not a boolean")
            confidence, threshold = finite(s["confidence"]), finite(s["threshold_used"])
            enabled = [c for c in checks if not c["disabled"]]
            total = finite(sum(c["score"] * c["weight"] for c in enabled)
                           / sum(c["weight"] for c in enabled))
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            problems[s["id"]] = f"inputs not usable: {type(exc).__name__}"
            continue
        worst = max(worst, abs(abs(total) - confidence))
        expected = "HOLD" if abs(total) < threshold else "BUY" if total > 0 else "SELL"
        why = []
        if abs(abs(total) - confidence) > SCORE_TOLERANCE:
            why.append("confidence does not match the recomputed score")
        if abs(sum(c["weight"] for c in checks) - 1.0) > 1e-9:
            why.append("weights do not sum to 1")
        if any(abs((0 if c["disabled"] else c["score"] * c["weight"]) - c["contribution"])
               > SCORE_TOLERANCE for c in checks):
            why.append("a contribution is not score × weight")
        if s.get("action") != expected:
            why.append(f"action {s.get('action')} but the signed score gives {expected}")
        if setup.get(s["id"]) != s["confidence"]:
            why.append("the decision's score differs from the signal's confidence")
        if why:
            problems[s["id"]] = "; ".join(why)
    ok = signals and not problems
    return record("score accounting", PASS if ok else FAIL,
                  f"largest recomputation difference {worst:.2e}; "
                  f"{len(problems)} observation(s) do not reconcile",
                  {"max_difference": worst, "unreconciled": dict(list(problems.items())[:20])})


def archive(facts: dict, day: date) -> dict:
    rows, with_time = whole(facts.get("rows")), whole(facts.get("with_exchange_time"))
    future = whole(facts.get("exchange_after_capture"))
    other = whole(facts.get("exchange_other_date"))
    ok = rows > 0 and with_time == rows and future == 0 and other == 0
    return record("NSE archive timestamps", PASS if ok else FAIL,
                  f"{rows} rows, {with_time} with an exchange time, {future} later than their "
                  f"capture, {other} from another date", facts)


def vix_close(row: dict | None, day: date) -> dict:
    """The session's stored VIX close, judged on what the row can establish.

    `vix_daily` holds a value, a source and the time the row was written. It
    holds no market observation time. QuantDesk's own close collection writes
    one row per session, from the live reading, only once the market has
    closed, with a fixed source. A row consistent with that is accepted; a
    row that is not is UNKNOWN, because the storage time alone neither proves
    nor disproves that the value is the session's close. No storage-time
    window is applied: a late write is not a failure.
    """
    name = "session VIX close"
    if row is None:
        return record(name, FAIL, "no VIX close is stored for the session date")
    try:
        close = finite(row.get("close"))
    except ValueError:
        return record(name, FAIL, "the stored VIX close is not a valid number")
    if close <= 0:
        return record(name, FAIL, f"the stored VIX close is {close}")
    stored = ist(row["ingested_at"]) if row.get("ingested_at") else None
    evidence = {"close": close, "source": row.get("source"),
                "database_storage_time_ist": stored.isoformat() if stored else UNKNOWN,
                "market_observation_time": "UNKNOWN — not stored in vix_daily"}
    if row.get("source") != VIX_CLOSE_SOURCE:
        return record(name, UNKNOWN, f"source {row.get('source')!r} is not the one the "
                      "session's own close collection records; the row cannot be established "
                      "as the expected session close", evidence)
    if stored is None or stored.date() != day or stored < at(day, SESSION_CLOSE):
        return record(name, UNKNOWN, "the row was not written after the close on the session "
                      "date, which is when the close collection writes it; it cannot be "
                      "established as the expected session close", evidence)
    return record(name, PASS, f"close {close} from {row['source']}, written after the close on "
                  "the session date (storage time is not the observation time)", evidence)


def paper_activity(facts: dict, baseline: dict | None, contemporaneous: bool) -> dict:
    """Reconcile stored paper positions with the decisions that explain them.

    Works only from the session snapshot (`read_session`), the same one the
    rest of the report is built from: nothing is read a second time, so the
    qualification and the reported positions cannot disagree. Natural
    activity is expected to change the counts: every position opened in the
    session must belong to an `entered` decision of the session, and the
    other way round. An unreadable source is UNKNOWN and sets no baseline.
    """
    name = "paper activity reconciliation"
    needed = ["positions", "journal", "decisions"] + (["engine"] if contemporaneous else [])
    unread = [k for k in needed if facts.get(k) is None]
    if unread:
        raise ProbeError(f"could not read {', '.join(unread)}: "
                         + "; ".join(facts["unreadable"].get(k, "") for k in unread))
    stored, journal, decisions = facts["positions"], facts["journal"], facts["decisions"]
    totals = {"positions_total": whole(stored.get("total")),
              "positions_open": whole(stored.get("open")),
              "journal_total": whole(journal.get("total"))}
    opened = stored.get("session")
    if not isinstance(opened, list):
        raise ProbeError("the positions opened in the session were not reported")
    entered = [d for d in decisions if d.get("outcome") == ENTERED]
    entered_ids = sorted(str((_vector(d).get("decision_context") or {}).get("position_id"))
                         for d in entered)
    opened_ids = sorted(str(p.get("id")) for p in opened)
    evidence = {**totals, "opened_this_session": len(opened), "entered_decisions": len(entered),
                "baseline": baseline}
    if not baseline or any(baseline.get(k) is None for k in ("positions_total", "journal_total")):
        return record(name, UNKNOWN, "no trusted baseline to reconcile paper activity against",
                      evidence)
    problems = []
    grew = totals["positions_total"] - baseline["positions_total"]
    if grew != len(opened):
        problems.append(f"positions grew by {grew} since the baseline but {len(opened)} "
                        "opened this session")
    if entered_ids != opened_ids:
        problems.append(f"positions opened {opened_ids} do not match entered decisions "
                        f"{entered_ids}")
    if totals["journal_total"] != baseline["journal_total"]:
        problems.append(f"journal trades were {baseline['journal_total']} at the baseline, "
                        f"now {totals['journal_total']}")
    if contemporaneous:
        if "position" not in facts["engine"]:
            raise ProbeError("the paper engine did not report a position field")
        engine_open = 0 if facts["engine"]["position"] is None else 1
        if totals["positions_open"] != engine_open:
            problems.append(f"{totals['positions_open']} open stored, engine holds {engine_open}")
    return record(name, FAIL if problems else PASS,
                  "; ".join(problems) or "every stored position is explained by an entered "
                  "decision; nothing else changed", evidence)


def compare_counters(before: dict | None, after: dict, day: date) -> dict:
    """Whether the API's counters establish an undisturbed session.

    Two snapshots are comparable only if they name the same process and the
    same counting start, the first was taken before the open and the second
    after the close. A change that the counters cannot place in time stays
    UNKNOWN: nothing is inferred about when it happened.
    """
    name = "operational continuity (scheduler and feed)"
    opened, closed = at(day, SESSION_OPEN), at(day, SESSION_CLOSE)
    identity = after["identity"]
    if ist(after["taken_at_ist"]) < closed:
        return record(name, UNKNOWN, "the closing snapshot was taken before the close", after)
    starts = [ist(identity["counting_since"]), ist(identity["feed_started_at"])]
    started = max(starts)
    if before is None:
        if any(s.date() != day or s >= opened for s in starts):
            return record(name, UNKNOWN, "no pre-market counter snapshot of this API process "
                          "exists, and its counters did not start on the session date before "
                          "the open", after)
        before = {"identity": identity, "taken_at_ist": started.isoformat(),
                  "jobs": {j: dict.fromkeys(JOB_COUNTERS, 0) for j in REQUIRED_JOBS},
                  "feed": dict.fromkeys(FEED_COUNTERS, 0),
                  "basis": "the counters start at zero when the process starts counting"}
    elif before.get("identity") != identity:
        return record(name, UNKNOWN, "the API process or its counting start changed between "
                      "the snapshots (a restart); they cannot be compared",
                      {"before": before.get("identity"), "after": identity})
    elif ist(before["taken_at_ist"]) >= opened:
        return record(name, UNKNOWN, "the earlier snapshot was taken after the open", after)
    deltas = {f"{job}.{k}": whole(after["jobs"][job][k]) - whole(before["jobs"].get(job, {}).get(k))
              for job in REQUIRED_JOBS for k in JOB_COUNTERS}
    deltas.update({k: whole(after["feed"][k]) - whole(before["feed"].get(k))
                   for k in FEED_COUNTERS})
    evidence = {"identity": identity, "from_ist": before["taken_at_ist"],
                "to_ist": after["taken_at_ist"], "deltas": deltas,
                "basis": before.get("basis", "a pre-market snapshot of the same process")}
    if any(v < 0 for v in deltas.values()):
        return record(name, UNKNOWN, "a counter went down (a reset); the snapshots cannot be "
                      "compared", evidence)
    moved = {k: v for k, v in deltas.items() if v}
    if moved:
        return record(name, UNKNOWN, f"counters moved between the snapshots ({moved}); they "
                      "cannot place the event inside or outside market hours", evidence)
    return record(name, PASS, "the same process counted throughout, and no scheduler error, "
                  "skip or missed run, stale event, login failure or malformed tick was added "
                  "between the snapshots", evidence)


def candidates(signals: list[dict], decisions: list[dict], positions: dict | None,
               cfg) -> list[dict]:
    """Every decision whose trace says expiry_day passed, valid or not, and how far it went."""
    by_id = {s["id"]: s for s in signals}
    stored = {p.get("id"): p for p in (positions or {}).get("session") or []}
    found = []
    for d in decisions:
        if not claims_expiry_day_pass(d):
            continue
        vector = _vector(d)
        identity, context = vector.get("identity") or {}, vector.get("decision_context") or {}
        signal = by_id.get(identity.get("signal_id"))
        consistent = not trace_problems(d)
        problems = trace_problems(d)
        if not decision_is_sound(d, by_id):
            problems.append("evidence is not complete v2_decision_evidence/2 linked to a "
                            "session observation")
        if signal is not None and not producer_ok(signal, cfg):
            problems.append("the linked observation is not from the expected producer")
        if signal is not None and not config_ok(signal, d, cfg):
            problems.append("configuration identity differs from the accepted baseline")
        if consistent and not expiry_day_passed(d):
            problems.append("expiry_day is not enabled and passed")
        gates = gate_map(d)
        stages = {name: GATE_STATUS.get((gates.get(name) or {}).get("status"), UNKNOWN)
                  if consistent else UNKNOWN for name in DOWNSTREAM_GATES}
        position_id = context.get("position_id")
        if positions is None or not consistent:
            stages["paper_position"] = UNKNOWN
        elif position_id is None:
            stages["paper_position"] = ("NOT REACHED" if d.get("outcome") == "rejected"
                                        else UNKNOWN)
        else:
            stages["paper_position"] = "PASSED" if position_id in stored else UNKNOWN
        stages["submission_fill"] = ("NOT REACHED" if stages["paper_position"] == "NOT REACHED"
                                     else UNKNOWN)
        contract = _evidence(d).get("contract_evidence")
        found.append({
            "decision_id": d["id"], "signal_id": identity.get("signal_id"),
            "valid_early_review_trigger": not problems, "not_valid_because": problems,
            "outcome": d.get("outcome"), "code": d.get("code"), "stages": stages,
            # QuantDesk's own contract_evidence exactly as stored: quote universe,
            # alternatives, result, pick, levels. Nothing is added or inferred.
            "contract_evidence": contract if isinstance(contract, dict) else UNKNOWN,
            "selection_context": {k: context[k] for k in ("listed_expiries", "chosen_expiry",
                                                          "risk", "equity")
                                  if context.get(k) is not None},
            "stored_position": stored.get(position_id),
            "data_status": vector.get("data_status"),
            "bid_parser_evidence": "NOT ADVANCED — a selector attempt alone does not validate "
                                   "the parser; its consumed input and interpretation must be "
                                   "inspected in the stored contract evidence"})
    return found


def funnel(decisions: list[dict]) -> dict:
    sound = [d for d in decisions if not trace_problems(d)]
    reached = [GATE_ORDER.index(g) for d in sound for g, v in gate_map(d).items()
               if v.get("status") in ("passed", "failed")]
    return {"decision_codes": dict(collections.Counter(d.get("code") for d in decisions)),
            "actions": dict(collections.Counter(d.get("action") for d in decisions)),
            "outcomes": dict(collections.Counter(d.get("outcome") for d in decisions)),
            "furthest_gate_reached": GATE_ORDER[max(reached)] if reached else UNKNOWN,
            "selector_attempts": sum(1 for d in sound if gate_map(d)["contract_selection"][
                "status"] in ("passed", "failed")),
            "decisions_with_inconsistent_trace": len(decisions) - len(sound)}


def read_session(probe, day: date, contemporaneous: bool = True, captures=None) -> dict:
    """One snapshot of the session's stored evidence, read once.

    Every later step — the gates, the report fields, the UNKNOWN list, the
    paper reconciliation and the next baseline — uses this snapshot and
    nothing else. A source that cannot be read is None, never empty, and the
    failed read is kept in `unreadable`; it is not retried.
    """
    facts: dict = {"unreadable": {}, "read_attempts": []}
    readers = [("signals", lambda: probe.signals(day)), ("decisions", lambda: probe.decisions(day)),
               ("positions", lambda: probe.positions(day)), ("journal", probe.journal),
               ("schedule", lambda: probe.agent_schedule(day, captures))]
    if contemporaneous:
        readers.append(("engine", lambda: probe.api("/v2/status")))
    for key, reader in readers:
        try:
            facts[key] = reader()
            facts["read_attempts"].append({"source": key, "read": "ok"})
        except ProbeError as exc:
            facts[key] = None
            facts["unreadable"][key] = str(exc)
            facts["read_attempts"].append({"source": key, "read": "failed", "reason": str(exc)})
    return facts


def evidence_checks(probe, cfg, facts: dict, day: date, now: datetime, final: bool,
                    baseline: dict | None = None, contemporaneous: bool = True,
                    sink: list | None = None) -> list[dict]:
    """The collection and data gates. A failure or an unknown here means INCOMPLETE.

    Each finished check is also appended to `sink` as it completes, so a run
    stopped by its time limit keeps the ones it had finished.
    """
    signals, decisions = facts["signals"], facts["decisions"]

    def rows(name, body, *keys):
        def run():
            unread = [k for k in keys if facts.get(k) is None]
            if unread:
                raise ProbeError(f"could not read {', '.join(unread)}: "
                                 + "; ".join(facts["unreadable"].get(k, "") for k in unread))
            return body()
        return guarded(name, run)

    steps = [
        lambda: rows("collection coverage",
                     lambda: collection(signals, schedule_basis(
                         facts["schedule"], day, None if final else now), day, now, final),
                     "signals"),
        lambda: rows("producer revision", lambda: producer(signals, cfg), "signals"),
        lambda: rows("configuration identity", lambda: configuration(signals, decisions, cfg),
                     "signals", "decisions"),
        lambda: rows("observation/decision linkage and evidence status",
                     lambda: linkage(signals, decisions), "signals", "decisions"),
        lambda: rows("gate traces internally consistent", lambda: gate_traces(decisions),
                     "decisions"),
    ]
    if contemporaneous:
        steps.append(lambda: guarded("LIVE_TRADING=false", lambda: live_trading_off(probe)))
    if final:
        steps += [
            lambda: rows("option chain enabled with available OI on every observation",
                         lambda: option_chain(signals, cfg), "signals"),
            lambda: rows("score accounting", lambda: score_accounting(signals, decisions),
                         "signals", "decisions"),
            lambda: guarded("NSE archive timestamps",
                            lambda: archive(probe.archive(day), day)),
            lambda: guarded("session VIX close", lambda: vix_close(probe.vix_close(day), day)),
            lambda: guarded("paper activity reconciliation",
                            lambda: paper_activity(facts, baseline, contemporaneous)),
        ]
    checks = []
    for step in steps:
        checks.append(step())
        if sink is not None:
            sink.append(checks[-1])
    return checks
