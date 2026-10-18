"""Independent monitoring for scheduled VM operation: Azure runbooks, alerts and hosted checks.

Synthetic only. The runbooks' logic is exercised with fake HTTP answers; the
hosted-check and Automation schedules are evaluated from the prepared
configuration files, not from any live service. No check is created and no
ping is sent.
"""
import importlib.util
import io
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWER = ROOT / "scripts/native/power"
OBSERVER = ROOT / "scripts/native/observer"
sys.path.insert(0, str(OBSERVER))
sys.path.insert(0, str(POWER))

from quantdesk_observer import cli as observer_cli  # noqa: E402
from quantdesk_power import special  # noqa: E402
from quantdesk_power import status as power_status  # noqa: E402
from quantdesk_power.facts import IST  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(name, POWER / "azure" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qd_monitor = load("qd_monitor")
qd_start = load("qd_start")
VM = ("/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/quantdesk-rg/"
      "providers/Microsoft.Compute/virtualMachines/quantdesk-vm")


def at(text, day="2026-10-27"):                  # a Tuesday
    return datetime.fromisoformat(f"{day}T{text}+05:30")


# -- the independent monitor's policy ------------------------------------------------------
@pytest.mark.parametrize("when, state, problem", [
    ("07:55", "running", None),
    ("07:55", "deallocated", "failed startup"),            # the start did not happen
    ("07:55", "starting", "failed startup"),
    ("13:30", "deallocated", "unexpected stop"),
    ("15:50", "stopped", "unexpected stop"),
    ("21:30", "deallocated", None),
    ("21:30", "running", "did not complete"),              # never ran, blocked or unresolved
    ("21:30", "stopped", "incomplete shutdown"),           # stopped but still allocated
    ("21:30", "deallocating", "incomplete shutdown"),
    ("01:00", "running", "did not complete"),              # the reminder overnight
    ("07:15", "running", "did not complete"),              # and again before the start
    ("19:00", "running", None),                            # the shutdown window: not judged
    ("21:30", "unknown", "unknown"),
])
def test_the_monitor_judges_the_power_state_against_the_operating_policy(when, state, problem):
    found = qd_monitor.evaluate(at(when), state, {}, [])
    if problem is None:
        assert found == []
    else:
        assert len(found) == 1 and problem in found[0]


def test_weekends_expect_deallocation_unless_a_special_session_or_a_hold():
    saturday = at("12:00", "2026-10-31")
    assert "did not complete" in qd_monitor.evaluate(saturday, "running", {}, [])[0]
    assert qd_monitor.evaluate(saturday, "deallocated", {}, []) == []
    hold = {"qd-power-hold": "2026-11-01T09:00:00+05:30"}
    assert qd_monitor.evaluate(saturday, "running", hold, []) == []
    expired = {"qd-power-hold": "2026-10-31T09:00:00+05:30"}
    assert qd_monitor.evaluate(saturday, "running", expired, []) != []


def test_a_hold_never_excuses_a_stopped_vm_in_the_daytime():
    hold = {"qd-power-hold": "2026-10-28T09:00:00+05:30"}
    assert "unexpected stop" in qd_monitor.evaluate(at("10:30"), "deallocated", hold, [])[0]


def test_a_special_session_has_its_own_monitored_window():
    record = json.loads((POWER / "config/special_session.example.json").read_text())
    entry = special.plan(record)["monitor"]["variable_entry"]
    sunday = "2026-11-08"
    assert qd_monitor.evaluate(at("18:30", sunday), "running", {}, [entry]) == []
    assert "special session" in qd_monitor.evaluate(at("17:20", sunday), "deallocated", {},
                                                    [entry])[0]
    assert qd_monitor.evaluate(at("23:00", sunday), "deallocated", {}, [entry]) == []
    assert "unreadable" in qd_monitor.evaluate(at("12:00", sunday), "deallocated", {},
                                               [{"date": sunday}])[0]


def test_power_state_is_read_from_exactly_one_status_code():
    view = {"statuses": [{"code": "ProvisioningState/succeeded"},
                         {"code": "PowerState/deallocated"}]}
    assert qd_monitor.power_state(view) == "deallocated"
    assert qd_monitor.power_state({"statuses": []}) == "unknown"
    two = {"statuses": [{"code": "PowerState/running"}, {"code": "PowerState/stopped"}]}
    assert qd_monitor.power_state(two) == "unknown"


KEY = bytes(range(32))
BOOT = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"


def status_text(when, seq=1, state="RESTORED", flag="OK", sub="-", boot=BOOT, vm=None,
                key=KEY, txn="a" * 32, cycle=None):
    cycle = cycle or power_status.cycle_text(power_status.cycle_start(when, []))
    return power_status.encode({"schema": "qdps1", "vm": vm or power_status.vm_digest(VM),
                                "boot": boot, "seq": seq, "txn": txn, "state": state,
                                "sub": sub, "flag": flag, "cycle": cycle,
                                "epoch": int(when.timestamp())}, key)


def signed_opener(text):
    def opener(request, timeout):
        if "msi" in request.full_url:
            return Answer({"access_token": "t"})
        tags = {} if text is None else {"qd-power-status": text}
        return Answer({"properties": {"instanceView": {"statuses": [
            {"code": "PowerState/running"}]}}, "tags": tags})
    return opener


class Answer(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(json.dumps(body).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_failing_monitor_run_fails_its_job_which_is_the_alert(monkeypatch):
    monkeypatch.setenv("QD_VM_RESOURCE_ID", VM)
    monkeypatch.setenv("QD_SPECIAL_SESSIONS", "[]")
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "secret")

    def opener(request, timeout):
        if "msi" in request.full_url:
            return Answer({"access_token": "t"})
        return Answer({"properties": {"instanceView": {"statuses": [
            {"code": "PowerState/running"}]}}, "tags": {}})
    with pytest.raises(RuntimeError, match="QUANTDESK VM ALERT"):
        qd_monitor.main(now=at("21:30"), opener=opener)
    # Correction (pass 20): without a verifiable shutdown status the run is an alert,
    # never a pass; with a fresh signed status the same daytime run passes.
    with pytest.raises(RuntimeError, match="key is not configured"):
        qd_monitor.main(now=at("10:30"), opener=signed_opener(status_text(at("10:00"))))
    monkeypatch.setenv("QD_POWER_STATUS_KEY", KEY.hex())
    monkeypatch.delenv("QD_POWER_STATUS_LAST", raising=False)
    with pytest.raises(RuntimeError, match="no shutdown status"):
        qd_monitor.main(now=at("10:30"), opener=signed_opener(None))
    assert qd_monitor.main(now=at("10:30"), opener=signed_opener(
        status_text(at("10:00")))) == 0
    assert qd_monitor.os.environ["QD_POWER_STATUS_LAST"] == status_text(at("10:00"))
    monkeypatch.setenv("QD_VM_RESOURCE_ID", VM + "/../other")
    with pytest.raises(RuntimeError, match="malformed"):
        qd_monitor.main(now=at("10:30"), opener=opener)


# -- the start runbook ----------------------------------------------------------------------
class Azure:
    def __init__(self, states, start_status=202):
        self.states, self.start_status, self.posts = list(states), start_status, []

    def __call__(self, request, timeout):
        if request.get_method() == "POST":
            self.posts.append(request.full_url)
            return Answer({}, self.start_status)
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return Answer({"statuses": [{"code": f"PowerState/{state}"}]})


def test_the_start_runbook_starts_a_deallocated_vm_and_waits_for_running():
    azure = Azure(["deallocated", "starting", "running"])
    assert qd_start.start(VM, "t", azure, sleep=lambda s: None) == "started"
    assert len(azure.posts) == 1 and azure.posts[0].endswith("/start?api-version=2024-07-01")


def test_the_start_runbook_does_nothing_to_a_running_vm():
    azure = Azure(["running"])
    assert qd_start.start(VM, "t", azure, sleep=lambda s: None).startswith("already")
    assert azure.posts == []


def test_a_start_that_never_reaches_running_fails_the_job():
    azure = Azure(["deallocated", "starting"])
    clock = iter(range(0, 10000, 30))
    with pytest.raises(RuntimeError, match="ten minutes"):
        qd_start.start(VM, "t", azure, sleep=lambda s: None, monotonic=lambda: next(clock))
    with pytest.raises(RuntimeError, match="not starting"):
        qd_start.start(VM, "t", Azure(["deallocating"]), sleep=lambda s: None)


def test_the_runbooks_agree_on_the_management_api():
    assert qd_start.API_VERSION == qd_monitor.API_VERSION and qd_start.ARM == qd_monitor.ARM
    assert qd_start.VM_ID.pattern == qd_monitor.VM_ID.pattern


# -- Azure roles, schedules and alerts --------------------------------------------------------
def test_each_identity_can_do_only_its_own_part_on_one_vm():
    start = json.loads((POWER / "azure/role-automation.json").read_text())
    dealloc = json.loads((POWER / "azure/role-vm-deallocate.json").read_text())
    assert set(start["Actions"]) == {"Microsoft.Compute/virtualMachines/read",
                                     "Microsoft.Compute/virtualMachines/instanceView/read",
                                     "Microsoft.Compute/virtualMachines/start/action"}
    assert set(dealloc["Actions"]) == {"Microsoft.Compute/virtualMachines/read",
                                       "Microsoft.Compute/virtualMachines/deallocate/action"}
    for role in (start, dealloc):
        assert not any("*" in a or a.endswith("/write") or "runCommand" in a
                       for a in role["Actions"])
        assert role["DataActions"] == [] and len(role["AssignableScopes"]) == 1
        assert "/virtualMachines/" in role["AssignableScopes"][0]


def weekly_runs(runbook):
    schedules = json.loads((POWER / "azure/schedules.json").read_text())["schedules"]
    names = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4,
             "Saturday": 5, "Sunday": 6}
    runs = []
    for s in schedules:
        if s["runbook"] != runbook:
            continue
        assert s["timeZone"] == "Asia/Kolkata" and s["frequency"] == "Week"
        for day in s["weekDays"]:
            runs.append(datetime.combine(date(2026, 10, 26) + timedelta(days=names[day]),
                                         time.fromisoformat(s["time"]), tzinfo=IST))
    return sorted(runs)


def test_the_vm_starts_at_0730_on_every_weekday_holidays_included():
    starts = weekly_runs("qd-start")
    assert [r.weekday() for r in starts] == [0, 1, 2, 3, 4]
    assert {r.time() for r in starts} == {time(7, 30)}


def test_the_missing_monitor_alert_cannot_be_triggered_by_the_schedule_itself():
    # Correction (pass 20): the rule is now the log search alert in the proposal file;
    # the metric rule could not see a monitor that never ran (no samples).
    alerts = json.loads((POWER / "azure/alerts.monitor-missing.proposal.json").read_text())
    missing = next(r for r in alerts["resources"] if r["name"] == "qd-monitor-missing")
    window = missing["properties"]["windowSize"]
    assert window == "PT12H"
    runs = weekly_runs("qd-monitor")
    runs.append(runs[0] + timedelta(days=7))
    largest = max(b - a for a, b in zip(runs, runs[1:], strict=False))
    assert largest < timedelta(hours=12)


def test_the_monitor_runs_at_every_point_the_policy_judges():
    times = {r.time() for r in weekly_runs("qd-monitor") if r.weekday() < 5}
    assert {time(7, 55), time(21, 30), time(1, 0), time(7, 15)} <= times
    assert any(time(9, 15) <= t <= time(15, 30) for t in times)


def test_failed_jobs_and_failed_vm_operations_email_the_owner():
    alerts = {r["name"]: r for r in json.loads(
        (POWER / "azure/alerts.template.json").read_text())["resources"]}
    group = alerts["qd-owner-email"]["properties"]
    assert group["emailReceivers"][0]["emailAddress"] == "[parameters('ownerEmail')]"
    failed = alerts["qd-runbook-failed"]["properties"]["criteria"]["allOf"][0]
    dims = {d["name"]: d["values"] for d in failed["dimensions"]}
    # Correction (pass 20): Azure Automation's TotalJob dimension is "Runbook".
    assert "Failed" in dims["Status"] and set(dims["Runbook"]) == {"qd-start", "qd-monitor"}
    assert alerts["qd-runbook-failed"]["properties"]["autoMitigate"] is False
    operation = alerts["qd-vm-operation-failed"]["properties"]["condition"]["allOf"]
    names = {c["equals"] for c in operation[2]["anyOf"]}
    assert names == {"Microsoft.Compute/virtualMachines/deallocate/action",
                     "Microsoft.Compute/virtualMachines/start/action"}


# -- the hosted checks ------------------------------------------------------------------------
def oncalendar_times(line: str, day: date) -> list[time]:
    """The small subset of OnCalendar used here: 'Mon..Fri HH:MM/S' and 'Mon..Fri HH..HH:MM/S'."""
    days, clock = line.split()
    assert days == "Mon..Fri"
    if day.weekday() >= 5:
        return []
    hours, minutes = clock.split(":")
    start_minute, step = (int(x) for x in minutes.split("/"))
    first, last = (int(h) for h in hours.split("..")) if ".." in hours else (int(hours),) * 2
    return [time(h, m) for h in range(first, last + 1) for m in range(start_minute, 60, step)]


def hosted():
    return {c["name"]: c for c in json.loads(
        (POWER / "monitoring/healthchecks.json").read_text())["checks"]}


def test_the_four_original_identities_are_preserved():
    from quantdesk_observer import healthchecks
    assert set(hosted()) == set(healthchecks.CHECKS) == {"liveness", "premarket", "midsession",
                                                         "afterclose"}


def test_the_first_heartbeat_is_due_0745_and_alerts_by_0755_and_nights_are_silent():
    liveness = hosted()["liveness"]
    grace = timedelta(seconds=liveness["grace_seconds"])
    lines = liveness["schedule"].splitlines()
    tuesday = date(2026, 10, 27)
    expected = sorted(t for line in lines for t in oncalendar_times(line, tuesday))
    assert expected[0] == time(7, 45)
    first = datetime.combine(tuesday, expected[0], tzinfo=IST)
    assert first + grace == datetime.combine(tuesday, time(7, 55), tzinfo=IST)
    assert expected[-1] == time(16, 55)
    # Every expected heartbeat falls while the VM is up: started 07:30, no shutdown before 17:30.
    last_alert = datetime.combine(tuesday, expected[-1], tzinfo=IST) + grace
    assert last_alert.time() < time(17, 30)
    gaps = [datetime.combine(tuesday, b) - datetime.combine(tuesday, a)
            for a, b in zip(expected, expected[1:], strict=False)]
    assert max(gaps) == timedelta(minutes=5)
    assert all(oncalendar_times(line, date(2026, 10, 31)) == [] for line in lines)


@pytest.mark.parametrize("check", ["premarket", "midsession", "afterclose"])
def test_the_assessment_deadlines_are_the_observers_own(check):
    found = hosted()[check]
    minute, hour = found["schedule"].split()[:2]
    start = datetime.combine(date(2026, 10, 27), time(int(hour), int(minute)))
    deadline = start + timedelta(seconds=found["grace_seconds"])
    window = observer_cli.DEFAULTS["schedule"][check]
    assert f"{start:%H:%M}" == window["at"] and f"{deadline:%H:%M}" == window["deadline"]
    assert found["schedule"].endswith("* * 1-5")


def test_overnight_incidents_persist_and_are_repeated():
    semantics = json.loads((POWER / "monitoring/healthchecks.json").read_text())[
        "incident_semantics"]
    assert "stays down overnight" in semantics["persistence"]
    assert "no success is ever sent late" in semantics["recovery"]
    assert "blocks every shutdown" in semantics["undelivered"]
    # The independent repeat: every overnight monitor run reports a VM left running.
    alerts = [qd_monitor.evaluate(at(t), "running", {}, []) for t in ("21:30",)] + \
             [qd_monitor.evaluate(at(t, "2026-10-28"), "running", {}, []) for t in
              ("01:00", "07:15")]
    assert all(found for found in alerts)


def test_a_special_session_states_its_monitoring_deadlines_and_manual_hosted_lines():
    record = json.loads((POWER / "config/special_session.example.json").read_text())
    plan = special.plan(record)
    lines = plan["healthchecks_manual"]["add_oncalendar_lines"]
    assert lines[0] == "2026-11-08 17:20,25,30,35,40,45,50,55"
    assert lines[1] == "2026-11-08 18..19:00/5"
    assert "never changes or pauses a hosted check" in plan["healthchecks_manual"]["note"]
    assert len("\n".join(lines + hosted()["liveness"]["schedule"].splitlines())) <= 100


# == pass 20 corrections ===========================================================================
def judge(now, state, text, last=None, key=KEY):
    return qd_monitor.evaluate_status(now, VM, state, {"qd-power-status": text} if text else {},
                                      key, last)


def test_the_status_codec_is_identical_on_both_sides():
    text = status_text(at("10:00"), seq=7, state="UNKNOWN", flag="UNRESOLVED", sub=BOOT[:8])
    mine, theirs = power_status.decode(text, KEY), qd_monitor.decode_status(text, KEY)
    assert mine == theirs and theirs["seq"] == 7 and len(text) <= 256
    assert power_status.FIELDS == qd_monitor.STATUS_FIELDS
    assert power_status.PATTERNS == qd_monitor.STATUS_PATTERNS
    assert power_status.FLAGS == qd_monitor.STATUS_FLAGS
    assert power_status.vm_digest(VM) == qd_monitor.vm_digest(VM)
    longest = status_text(at("10:00"), seq=9999999999, state="OPERATOR_CONFIRMED_NOT_PERFORMED",
                          flag="RESTORE_FAILED", sub="ffffffff")
    assert len(longest) <= 256


def test_a_fresh_genuine_status_passes_and_is_remembered():
    text = status_text(at("10:00"))
    assert judge(at("10:30"), "running", text) == ([], text)
    done = status_text(at("21:00"), state="COMPLETED", sub=BOOT[:8])
    assert judge(at("23:00"), "deallocated", done) == ([], done)
    later = status_text(at("07:41", "2026-10-28"), seq=2, state="COMPLETED", sub=BOOT[:8],
                        boot="1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
    assert judge(at("07:55", "2026-10-28"), "running", later, last=done) == ([], later)


@pytest.mark.parametrize("make, why", [
    (lambda: (None, None, KEY), "no shutdown status"),
    (lambda: (status_text(at("10:00")), None, None), "key is not configured"),
    (lambda: (status_text(at("10:00"), key=b"x" * 32), None, KEY), "does not verify"),
    (lambda: (status_text(at("10:00")) + "0", None, KEY), "unusable"),
    (lambda: (status_text(at("10:00")).replace("RESTORED", "COMPLETED"), None, KEY),
     "does not verify"),
    (lambda: (status_text(at("10:00"), vm="0" * 12), None, KEY), "another VM"),
    (lambda: (status_text(at("11:00")), None, KEY), "future"),
    (lambda: (status_text(at("07:00")), None, KEY), "stale"),
    (lambda: (status_text(at("10:00"), seq=3), status_text(at("09:00"), seq=4), KEY), "replay"),
    (lambda: (status_text(at("10:00"), seq=4), status_text(at("09:59"), seq=4), KEY),
     "two different"),
    (lambda: (status_text(at("10:00")), "garbage", KEY), "replay protection is lost"),
    (lambda: ("x" * 300, None, KEY), "unusable"),
])
def test_a_missing_forged_stale_replayed_or_conflicting_status_is_an_alert(make, why):
    text, last, key = make()
    problems, remembered = judge(at("10:30"), "running", text, last=last, key=key)
    # Correction (pass 21): a status can now have several problems (its cycle as well).
    assert problems and any(why in p for p in problems)
    # Only an authentic, newer record is remembered (a stale one is authentic but old).
    assert remembered == (text if why == "stale" else None)


@pytest.mark.parametrize("flag, state", [("UNRESOLVED", "UNKNOWN"), ("UNRESOLVED", "ACCEPTED"),
                                         ("UNRESOLVED", "SUBMITTING"),
                                         ("RESTORE_FAILED", "RESTORE_FAILED"),
                                         ("CORRUPT", "NONE")])
@pytest.mark.parametrize("power", ["deallocated", "running", "stopped"])
def test_an_unresolved_incident_alerts_through_planned_downtime(flag, state, power):
    text = status_text(at("21:00"), state=state, flag=flag,
                       sub=BOOT[:8] if state != "RESTORE_FAILED" else "-")
    for when in (at("21:30"), at("01:00", "2026-10-28"), at("07:15", "2026-10-28")):
        problems, remembered = judge(when, power, text, last=text)
        assert any(flag in p for p in problems) and remembered == text


def test_a_vm_deallocated_outside_the_controller_or_restarted_without_a_status_alerts():
    restored = status_text(at("18:00"), state="RESTORED")
    assert "outside the controller" in judge(at("21:30"), "deallocated", restored)[0][0]
    done = status_text(at("21:00"), state="COMPLETED", sub=BOOT[:8])
    problems = judge(at("21:50"), "running", done)[0]
    assert any("no status since the restart" in p for p in problems)


def test_the_monitor_job_fails_on_a_forged_status_and_keeps_the_last_good_one(monkeypatch):
    monkeypatch.setenv("QD_VM_RESOURCE_ID", VM)
    monkeypatch.setenv("QD_SPECIAL_SESSIONS", "[]")
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "secret")
    monkeypatch.setenv("QD_POWER_STATUS_KEY", KEY.hex())
    good = status_text(at("10:00"), seq=5)
    monkeypatch.setenv("QD_POWER_STATUS_LAST", good)
    with pytest.raises(RuntimeError, match="does not verify"):
        qd_monitor.main(now=at("10:30"), opener=signed_opener(status_text(at("10:00"), seq=6,
                                                                          key=b"y" * 32)))
    with pytest.raises(RuntimeError, match="replay"):
        qd_monitor.main(now=at("10:30"), opener=signed_opener(status_text(at("09:00"), seq=4)))
    assert qd_monitor.os.environ["QD_POWER_STATUS_LAST"] == good


# -- metric samples and the missing-monitor alert -------------------------------------------------
def metric_alert_fires(rule, samples):
    """Azure metric alert semantics for this purpose: no samples, nothing to evaluate."""
    criterion = rule["properties"]["criteria"]["allOf"][0]
    if not samples:
        return False
    total = sum(samples)
    return total > criterion["threshold"] if criterion["operator"] == "GreaterThan" \
        else total < criterion["threshold"]


def log_alert_fires(rule, rows):
    """`... | summarize completed = count()` over `rows` matching rows, then the criterion."""
    criterion = rule["properties"]["criteria"]["allOf"][0]
    query = criterion["query"]
    assert query.rstrip().endswith("summarize completed = count()") and " by " not in query
    result = [{criterion["metricMeasureColumn"]: len(rows)}]    # one row, even for no input
    value = sum(r[criterion["metricMeasureColumn"]] for r in result)
    assert criterion["operator"] == "LessThan" and criterion["timeAggregation"] == "Total"
    return value < criterion["threshold"]


def test_a_monitor_that_never_runs_is_caught_even_with_no_metric_samples():
    template = json.loads((POWER / "azure/alerts.template.json").read_text())
    for rule in template["resources"]:
        if rule["type"] == "Microsoft.Insights/metricAlerts":
            for criterion in rule["properties"]["criteria"]["allOf"]:
                # No metric rule is relied on to see absence: with no samples it cannot fire.
                assert criterion["operator"] != "LessThan", rule["name"]
                assert not metric_alert_fires(rule, [])
    proposal = json.loads((POWER / "azure/alerts.monitor-missing.proposal.json").read_text())
    [rule] = proposal["resources"]
    assert rule["type"] == "Microsoft.Insights/scheduledQueryRules"
    assert "NOT AUTHORIZED" in proposal["metadata"]["status"]
    assert log_alert_fires(rule, []) is True                       # nothing ran at all
    assert log_alert_fires(rule, [{"ResultType": "Completed"}]) is False
    assert '"qd-monitor"' in rule["properties"]["criteria"]["allOf"][0]["query"]
    assert rule["properties"]["windowSize"] == "PT12H"


def test_runbook_alerts_use_azures_runbook_dimension():
    text = (POWER / "azure/alerts.template.json").read_text()
    assert "RunbookName" not in text
    failed = next(r for r in json.loads(text)["resources"] if r["name"] == "qd-runbook-failed")
    dims = {d["name"] for d in failed["properties"]["criteria"]["allOf"][0]["dimensions"]}
    assert dims == {"Status", "Runbook"}


def test_no_fifth_hosted_check_and_no_new_cloud_permission():
    assert len(hosted()) == 4
    dealloc = json.loads((POWER / "azure/role-vm-deallocate.json").read_text())
    assert not any("tags" in a or "operations" in a for a in dealloc["Actions"])


# == pass 21: the status is bound to its operating cycle ==========================================
FRIDAY, MONDAY = "2026-10-30", "2026-11-02"
SPECIAL = json.loads((POWER / "config/special_session.example.json").read_text())
SPECIAL_ENTRY = special.plan(SPECIAL)["monitor"]["variable_entry"]


def weekend_shutdown():
    return status_text(at("21:00", FRIDAY), state="COMPLETED", sub=BOOT[:8])


@pytest.mark.parametrize("when, day", [("21:30", FRIDAY), ("01:00", "2026-10-31"),
                                       ("12:00", "2026-10-31"), ("18:00", "2026-11-01"),
                                       ("07:15", MONDAY), ("07:45", MONDAY)])
def test_legitimate_overnight_and_weekend_downtime_stays_quiet(when, day):
    problems, _ = judge(at(when, day), "deallocated", weekend_shutdown())
    assert problems == []


@pytest.mark.parametrize("when, day", [("07:55", MONDAY), ("10:30", MONDAY), ("21:30", MONDAY),
                                       ("01:00", "2026-11-03")])
def test_an_old_record_reused_after_a_required_startup_is_an_alert(when, day):
    problems, _ = judge(at(when, day), "deallocated", weekend_shutdown())
    assert any("no evidence of that startup" in p for p in problems)


def test_a_running_vm_needs_a_record_from_this_cycle_even_if_recent():
    # Published at 07:20 (yesterday's cycle), judged at 07:55: under the age limit, yet
    # nothing shows today's 07:30 startup happened.
    text = status_text(at("07:20", "2026-10-28"))
    problems, _ = judge(at("07:55", "2026-10-28"), "running", text)
    assert problems and "startup was required" in problems[0]
    fresh = status_text(at("07:41", "2026-10-28"), seq=2)
    assert judge(at("07:55", "2026-10-28"), "running", fresh, last=text) == ([], fresh)


def test_the_previous_cycle_is_accepted_only_while_the_startup_is_under_way():
    text = status_text(at("21:00", "2026-10-27"), state="COMPLETED", sub=BOOT[:8])
    assert judge(at("07:45", "2026-10-28"), "deallocated", text)[0] == []
    assert judge(at("07:50", "2026-10-28"), "deallocated", text)[0] != []


def test_a_special_session_is_a_required_startup_of_its_own():
    sunday = "2026-11-08"
    friday_record = status_text(at("21:00", "2026-11-06"), state="COMPLETED", sub=BOOT[:8])
    assert judge(at("12:00", sunday), "deallocated", friday_record)[0] == []
    with_special = qd_monitor.evaluate_status(at("18:30", sunday), VM, "running",
                                              {"qd-power-status": friday_record}, KEY, None,
                                              [SPECIAL_ENTRY])[0]
    assert any("startup was required at Sun 2026-11-08 17:00" in p for p in with_special)
    session = status_text(at("17:41", sunday), seq=2, cycle="202611081700")
    assert qd_monitor.evaluate_status(at("18:30", sunday), VM, "running",
                                      {"qd-power-status": session}, KEY, friday_record,
                                      [SPECIAL_ENTRY]) == ([], session)


@pytest.mark.parametrize("specials", ["not a list", [{"date": "2026-11-08"}],
                                      [{**SPECIAL_ENTRY, "vm_start": "17:00"}]])
def test_an_unusable_special_session_list_exempts_nothing(specials):
    problems, _ = qd_monitor.evaluate_status(at("23:00", FRIDAY), VM, "deallocated",
                                             {"qd-power-status": weekend_shutdown()}, KEY,
                                             None, specials)
    assert any("no downtime is exempted" in p for p in problems)


def test_a_weekday_holiday_is_never_an_exemption():
    # 2026-11-10 is treated like any weekday: the 07:30 start is required, calendar or not.
    text = status_text(at("21:00", "2026-11-09"), state="COMPLETED", sub=BOOT[:8])
    assert judge(at("21:30", "2026-11-10"), "deallocated", text)[0] != []


@pytest.mark.parametrize("cycle, why", [("-", "names no operating cycle"),
                                        ("202610271730", "before its own cycle"),
                                        ("202610290730", "before its own cycle")])
def test_a_missing_or_impossible_cycle_is_an_alert(cycle, why):
    text = status_text(at("10:00"), cycle=cycle)
    problems, _ = judge(at("10:30"), "running", text)
    assert any(why in p for p in problems)


def test_both_sides_compute_the_same_cycle_everywhere():
    starts = [datetime.fromisoformat(SPECIAL["vm_start_ist"])]
    moment = at("00:00", "2026-10-24")
    while moment < at("00:00", "2026-11-16"):
        for specials in ([], starts):
            assert power_status.cycle_start(moment, specials) == \
                qd_monitor.cycle_start(moment, specials)
        moment += timedelta(minutes=37)
    assert power_status.cycle_text(power_status.cycle_start(at("07:29", MONDAY), [])) == \
        "202610300730"


def test_resume_failed_is_an_alert_flag_and_a_settled_deallocation_is_not():
    failed = status_text(at("08:10"), state="RESUME_FAILED", flag="RESUME_FAILED",
                         sub="1a2b3c4d")
    assert any("RESUME_FAILED" in p for p in judge(at("08:30"), "running", failed)[0])
    assert power_status.FLAGS == qd_monitor.STATUS_FLAGS


def test_the_monitor_never_moves_its_remembered_status_backwards(monkeypatch):
    monkeypatch.setenv("QD_VM_RESOURCE_ID", VM)
    monkeypatch.setenv("QD_SPECIAL_SESSIONS", "[]")
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "secret")
    monkeypatch.setenv("QD_POWER_STATUS_KEY", KEY.hex())
    newer = status_text(at("10:00"), seq=9)
    monkeypatch.setenv("QD_POWER_STATUS_LAST", newer)
    with pytest.raises(RuntimeError):
        qd_monitor.main(now=at("10:30"), opener=signed_opener(status_text(at("10:10"), seq=8)))
    assert qd_monitor.os.environ["QD_POWER_STATUS_LAST"] == newer


# == pass 22 corrections ===========================================================================
# -- A4. the special-session example schema -------------------------------------------------------
SPECIAL_RECORD = json.loads((POWER / "config/special_session.example.json").read_text())
SPECIAL_EXAMPLE = json.loads((POWER / "azure/special-sessions.example.json").read_text())


def test_the_special_session_examples_agree_and_are_complete():
    entry = special.plan(SPECIAL_RECORD)["monitor"]["variable_entry"]
    assert SPECIAL_EXAMPLE == [entry]                      # what plan-special emits, exactly
    assert set(entry) == set(qd_monitor.SPECIAL_FIELDS)
    assert qd_monitor.special_entry_problem(entry) is None
    assert qd_monitor.special_starts(SPECIAL_EXAMPLE) == [
        datetime.fromisoformat("2026-11-08T17:00:00+05:30")]
    sunday = "2026-11-08"
    assert qd_monitor.evaluate(at("18:30", sunday), "running", {}, SPECIAL_EXAMPLE) == []
    text = status_text(at("17:10", sunday), cycle="202611081700")
    assert qd_monitor.evaluate_status(at("17:40", sunday), VM, "running",
                                      {"qd-power-status": text}, KEY, None,
                                      SPECIAL_EXAMPLE) == ([], text)


@pytest.mark.parametrize("damage", [*(f"drop:{name}" for name in
                                      ("date", "vm_start", "expect_running_from",
                                       "expect_running_until")),
                                    "date:2026-11-8", "vm_start:2026-11-08T17:00:00",
                                    "vm_start:2026-11-09T17:00:00+05:30",
                                    "expect_running_until:soon", "not-an-object"])
def test_an_incomplete_special_session_entry_is_invalid_and_fails_closed(damage):
    if damage == "not-an-object":
        entry = "2026-11-08"
    else:
        entry = dict(SPECIAL_EXAMPLE[0])
        name, _, value = damage.partition(":")
        if name == "drop":
            del entry[value]
        else:
            entry[name] = value
    assert qd_monitor.special_entry_problem(entry)
    sunday = "2026-11-08"
    assert any("unreadable" in p for p in
               qd_monitor.evaluate(at("18:30", sunday), "running", {}, [entry]))
    assert qd_monitor.special_starts([entry]) is None
    text = status_text(at("17:10", sunday))
    problems, _ = qd_monitor.evaluate_status(at("17:40", sunday), VM, "running",
                                             {"qd-power-status": text}, KEY, None, [entry])
    assert any("no downtime is exempted" in p for p in problems)


@pytest.mark.parametrize("field", sorted(special.FIELDS - {"schema"}))
def test_a_special_session_record_missing_any_field_is_refused(field):
    record = {k: v for k, v in SPECIAL_RECORD.items() if k != field}
    with pytest.raises(special.PlanError):
        special.plan(record)


def test_an_absent_special_session_variable_is_an_alert_not_none(monkeypatch):
    monkeypatch.setenv("QD_VM_RESOURCE_ID", VM)
    monkeypatch.delenv("QD_SPECIAL_SESSIONS", raising=False)
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "secret")
    monkeypatch.setenv("QD_POWER_STATUS_KEY", KEY.hex())
    monkeypatch.delenv("QD_POWER_STATUS_LAST", raising=False)
    with pytest.raises(RuntimeError, match="QD_SPECIAL_SESSIONS"):
        qd_monitor.main(now=at("10:30"), opener=signed_opener(status_text(at("10:00"))))
    assert any("missing or not a list" in p for p in
               qd_monitor.evaluate(at("10:30"), "running", {}, None))
    monkeypatch.setenv("QD_SPECIAL_SESSIONS", "[]")             # explicitly none: quiet
    assert qd_monitor.main(now=at("10:30"), opener=signed_opener(status_text(at("10:00")))) == 0


# -- A5. a hold never suppresses failure, unresolved-operation or monitoring alerts ---------------
HOLD = {"qd-power-hold": "2026-11-30T09:00:00+05:30"}           # active through every case


def everything(now, power, text, tags=HOLD, specials=()):
    found = qd_monitor.evaluate(now, power, {**tags, "qd-power-status": text}, list(specials))
    problems, _ = qd_monitor.evaluate_status(now, VM, power, {**tags, "qd-power-status": text}
                                             if text else dict(tags), KEY, None, specials)
    return found + problems


@pytest.mark.parametrize("flag, state", [("UNRESOLVED", "UNKNOWN_RETAINED"),
                                         ("RESTORE_FAILED", "RESTORE_FAILED"),
                                         ("RESUME_FAILED", "RESUME_FAILED"),
                                         ("CORRUPT", "NONE"),
                                         ("IN_PROGRESS", "RESTORING"),
                                         ("IN_PROGRESS", "FROZEN")])
@pytest.mark.parametrize("when", [("23:00", "2026-10-27"), ("12:00", "2026-10-31"),
                                  ("10:30", "2026-10-28")])
def test_a_hold_never_hides_an_unresolved_or_unfinished_operation(flag, state, when):
    now = at(*when)
    text = status_text(now - timedelta(minutes=20), state=state, flag=flag,
                       sub=BOOT[:8] if flag == "UNRESOLVED" else "-")
    assert any(flag in p for p in everything(now, "running", text))


def test_in_progress_is_an_alert_flag_and_ok_is_not():
    assert "IN_PROGRESS" in qd_monitor.ALERT_FLAGS and "OK" not in qd_monitor.ALERT_FLAGS
    assert set(qd_monitor.ALERT_FLAGS) == set(qd_monitor.STATUS_FLAGS) - {"OK"}


def test_a_hold_never_hides_a_missed_startup_or_a_missing_status():
    monday = "2026-11-02"
    friday = status_text(at("21:00", "2026-10-30"), state="COMPLETED", sub=BOOT[:8])
    # Monday 10:00, the VM running under a hold, but its status is Friday's: no evidence of
    # Monday's required startup.
    assert any("no evidence of that startup" in p
               for p in everything(at("10:00", monday), "running", friday))
    assert any("unexpected stop" in p for p in everything(at("10:00", monday), "deallocated",
                                                          friday))
    assert any("no shutdown status" in p for p in everything(at("23:00"), "running", None))
    assert any("special session" in p for p in everything(at("18:30", "2026-11-08"),
                                                          "deallocated", None,
                                                          specials=SPECIAL_EXAMPLE))


# == pass 23 corrections ===========================================================================
# -- 2. special-session timing: aware, ordered, on its own date; else a configuration error -------
def special_entry(**changes):
    entry = dict(SPECIAL_EXAMPLE[0])       # 17:00 < 17:20 < 20:30 IST on 2026-11-08
    entry.update(changes)
    return entry


TIMING_FAULTS = {
    "start_equals_from": {"vm_start": "2026-11-08T17:20:00+05:30"},
    "from_equals_until": {"expect_running_from": "2026-11-08T20:30:00+05:30"},
    "start_after_from": {"vm_start": "2026-11-08T17:30:00+05:30"},
    "from_after_until": {"expect_running_from": "2026-11-08T21:00:00+05:30"},
    "fully_reversed": {"vm_start": "2026-11-08T20:30:00+05:30",
                       "expect_running_until": "2026-11-08T17:00:00+05:30"},
    "start_naive": {"vm_start": "2026-11-08T17:00:00"},
    "from_naive": {"expect_running_from": "2026-11-08T17:20:00"},
    "until_naive": {"expect_running_until": "2026-11-08T20:30:00"},
    "start_malformed": {"vm_start": "17:00"},
    "from_malformed": {"expect_running_from": "2026-11-08 5pm"},
    "until_malformed": {"expect_running_until": None},
    "start_wrong_date": {"vm_start": "2026-11-07T17:00:00+05:30"},
    "from_wrong_date": {"expect_running_from": "2026-11-09T17:20:00+05:30"},
    "until_crosses_midnight": {"expect_running_until": "2026-11-09T00:30:00+05:30"},
    "start_previous_evening": {"vm_start": "2026-11-07T23:50:00+05:30"},
    "date_disagrees_with_all": {"date": "2026-11-09"},
    "utc_until_is_next_day_in_ist": {"expect_running_until": "2026-11-08T19:00:00+00:00"},
}


@pytest.mark.parametrize("fault", sorted(TIMING_FAULTS))
def test_special_session_timing_faults_are_configuration_errors(fault):
    entry = special_entry(**TIMING_FAULTS[fault])
    assert qd_monitor.special_entry_problem(entry), fault
    problems = qd_monitor.evaluate(at("12:00", "2026-11-08"), "deallocated", {}, [entry])
    assert any(p.startswith(qd_monitor.CONFIG) and "unreadable" in p for p in problems)
    assert qd_monitor.special_starts([entry]) is None


@pytest.mark.parametrize("entry", [
    special_entry(),
    special_entry(vm_start="2026-11-08T17:19:59+05:30"),             # one second apart
    special_entry(expect_running_until="2026-11-08T17:20:01+05:30"),
    special_entry(vm_start="2026-11-08T11:30:00+00:00"),              # 17:00 IST, written in UTC
    special_entry(vm_start="2026-11-08T00:00:00+05:30",
                  expect_running_until="2026-11-08T23:59:59+05:30"),  # the whole IST day
])
def test_well_ordered_same_day_entries_are_valid_at_every_boundary(entry):
    assert qd_monitor.special_entry_problem(entry) is None
    assert qd_monitor.special_list_problems([entry]) == []
    assert qd_monitor.special_starts([entry]) == [datetime.fromisoformat(entry["vm_start"])]


def test_two_entries_for_one_date_contradict_and_exempt_nothing():
    first = special_entry()
    second = special_entry(vm_start="2026-11-08T18:00:00+05:30",
                           expect_running_from="2026-11-08T18:20:00+05:30")
    problems = qd_monitor.special_list_problems([first, second])
    assert len(problems) == 1 and "contradictory" in problems[0] and "2026-11-08" in problems[0]
    assert qd_monitor.special_starts([first, second]) is None
    text = status_text(at("17:10", "2026-11-08"), cycle="202611081700")
    found, _ = qd_monitor.evaluate_status(at("17:40", "2026-11-08"), VM, "running",
                                          {"qd-power-status": text}, KEY, None, [first, second])
    assert any("no downtime is exempted" in p for p in found)


def power_opener(state, text):
    def opener(request, timeout):
        if "msi" in request.full_url:
            return Answer({"access_token": "t"})
        return Answer({"properties": {"instanceView": {"statuses": [
            {"code": f"PowerState/{state}"}]}}, "tags": {"qd-power-status": text}})
    return opener


@pytest.fixture
def monitor_env(monkeypatch):
    monkeypatch.setenv("QD_VM_RESOURCE_ID", VM)
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "secret")
    monkeypatch.setenv("QD_POWER_STATUS_KEY", KEY.hex())
    monkeypatch.delenv("QD_POWER_STATUS_LAST", raising=False)
    return monkeypatch


@pytest.mark.parametrize("fault", ["start_equals_from", "until_crosses_midnight", "from_naive",
                                   "duplicate", "not_json"])
def test_an_invalid_special_session_list_fails_the_job_as_a_configuration_error(monitor_env,
                                                                                fault):
    # Sunday night, the VM deallocated after a submitted shutdown: with a valid list this
    # run is quiet. An invalid list must still fail the job - downtime is never exempted
    # silently.
    night = at("23:00", "2026-11-01")
    text = status_text(at("21:00", "2026-10-30"), state="COMPLETED", sub=BOOT[:8])
    monitor_env.setenv("QD_SPECIAL_SESSIONS", "[]")
    assert qd_monitor.main(now=night, opener=power_opener("deallocated", text)) == 0
    if fault == "not_json":
        raw = "[{"
    elif fault == "duplicate":
        raw = json.dumps([special_entry(), special_entry()])
    else:
        raw = json.dumps([special_entry(**TIMING_FAULTS[fault])])
    monitor_env.setenv("QD_SPECIAL_SESSIONS", raw)
    monitor_env.delenv("QD_POWER_STATUS_LAST", raising=False)
    with pytest.raises(qd_monitor.ConfigurationError, match="configuration error"):
        qd_monitor.main(now=night, opener=power_opener("deallocated", text))
    assert issubclass(qd_monitor.ConfigurationError, RuntimeError)   # still a failed job


def test_an_ordinary_alert_is_not_a_configuration_error(monitor_env):
    monitor_env.setenv("QD_SPECIAL_SESSIONS", "[]")
    with pytest.raises(RuntimeError) as raised:
        qd_monitor.main(now=at("10:30"), opener=power_opener("deallocated",
                                                             status_text(at("10:00"))))
    assert type(raised.value) is RuntimeError and "unexpected stop" in str(raised.value)


# == pass 24 corrections ===========================================================================
# -- B. special-session dates: canonical YYYY-MM-DD only, duplicates by the parsed date -----------
BAD_DATES = {
    "compact": "20261108",                    # date.fromisoformat accepts this form
    "iso_week": "2026-W45-7",                 # ... and this one (Sunday 2026-11-08)
    "invalid_day": "2026-02-30",
    "invalid_month": "2026-13-08",
    "single_digit_day": "2026-11-8",
    "padded": " 2026-11-08",
    "with_time": "2026-11-08T00:00",
    "slashes": "2026/11/08",
    "fullwidth_digits": "２０２６-11-08",
    "integer": 20261108,
    "null": None,
    "list": ["2026-11-08"],
}


def night_run(monitor_env, entries):
    """Sunday 23:00 IST with the VM deallocated after a submitted shutdown: quiet when the
    special-session list is valid. Runs the monitor's real main()."""
    night = at("23:00", "2026-11-01")
    text = status_text(at("21:00", "2026-10-30"), state="COMPLETED", sub=BOOT[:8])
    monitor_env.delenv("QD_POWER_STATUS_LAST", raising=False)
    monitor_env.setenv("QD_SPECIAL_SESSIONS", json.dumps(entries))
    return qd_monitor.main(now=night, opener=power_opener("deallocated", text))


@pytest.mark.parametrize("kind", sorted(BAD_DATES))
def test_a_non_canonical_session_date_is_a_configuration_error_through_main(monitor_env, kind):
    assert night_run(monitor_env, [special_entry()]) == 0          # the valid control is quiet
    assert qd_monitor.session_date(BAD_DATES[kind]) is None
    with pytest.raises(qd_monitor.ConfigurationError, match="canonical YYYY-MM-DD"):
        night_run(monitor_env, [special_entry(date=BAD_DATES[kind])])


def test_a_missing_session_date_is_a_configuration_error_through_main(monitor_env):
    entry = {k: v for k, v in special_entry().items() if k != "date"}
    with pytest.raises(qd_monitor.ConfigurationError, match="missing date"):
        night_run(monitor_env, [entry])


@pytest.mark.parametrize("second", ["identical", "other_window"])
def test_a_repeated_date_is_a_configuration_error_through_main(monitor_env, second):
    first = special_entry()
    other = dict(first) if second == "identical" else special_entry(
        vm_start="2026-11-08T18:00:00+05:30", expect_running_from="2026-11-08T18:20:00+05:30")
    with pytest.raises(qd_monitor.ConfigurationError, match="2026-11-08 is listed 2 times"):
        night_run(monitor_env, [first, other])
    assert qd_monitor.special_starts([first, other]) is None


def test_canonical_dates_parse_and_round_trip():
    for text in ("2026-11-08", "2028-02-29", "2026-01-01", "2026-12-31"):
        assert qd_monitor.session_date(text).isoformat() == text
    assert qd_monitor.session_date("2027-02-29") is None              # not a leap year


def test_an_invalid_date_exempts_no_downtime_even_inside_its_window(monitor_env):
    # The VM is deallocated during what would be the session window: with a bad date the
    # entry exempts nothing, and the job still fails (configuration error), never passes.
    text = status_text(at("21:00", "2026-11-06"), state="COMPLETED", sub=BOOT[:8])
    monitor_env.delenv("QD_POWER_STATUS_LAST", raising=False)
    monitor_env.setenv("QD_SPECIAL_SESSIONS", json.dumps([special_entry(date="20261108")]))
    with pytest.raises(qd_monitor.ConfigurationError):
        qd_monitor.main(now=at("18:30", "2026-11-08"), opener=power_opener("deallocated", text))
