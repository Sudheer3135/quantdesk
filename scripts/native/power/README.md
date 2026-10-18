# QuantDesk power controller (Option A scheduled operation)

A separate, fail-safe program that may deallocate the one QuantDesk Azure VM
after the day's work is done, and nothing else. **Prepared and tested locally
only.** Nothing here is installed, no account, role, identity, Automation
schedule, alert or hosted check exists, and nothing has contacted Azure.

## Operating policy (IST)

| Time | What |
|---|---|
| 07:30 Mon–Fri | Azure Automation `qd-start` starts the VM (holidays included) |
| 07:45 | first expected heartbeat; hosted alert by 07:55 if it is missing |
| 08:35–08:45 | morning assessment (observer, unchanged) |
| 12:30–12:45 | midday assessment (unchanged) |
| 16:00–16:30 | after-close assessment (unchanged) |
| 17:30 | first conditional shutdown attempt; then every 15 min until 21:00 |
| 21:30, 01:00, 07:15 | the Azure monitor expects the VM deallocated |
| hourly at :40 while up | reconciliation and a fresh signed status record |

Unfinished or uncertain work keeps the VM up and alerts. Nothing ever marks
an assessment PASS.

## Parts and identities

| Part | Runs as | Can |
|---|---|---|
| `quantdesk-power-request` (timer + oneshot) | `quantdesk-power`, no shell, no sudo, no network | read its own non-secret `request.json` (request directory and window only) and create a request file in `/run/quantdesk-power/requests` (mode 1730); the controller's configuration and state and the observer's configuration and state are inaccessible in its namespace |
| `quantdesk-power-controller` (path + oneshot) | root, sandboxed | reconcile, validate one request, run the transaction; the only process that reaches the metadata service; the observer's state is read-only to it and it opens exactly one existing file there, the observer's `lock`, read-only |
| `quantdesk-power-recover` (boot + hourly timer) | root | settle unfinished transactions; publish the status record |
| admission gate (`ExecStartPre=+` drop-in on the four workload units) | root, read-only | allow or refuse each start of PostgreSQL, Redis, the API and the dashboard |
| observer `shutdown-evidence` (v0.4.14) | `quantdesk-observer`, started by the controller | print sealed read-only evidence |
| `qd-start`, `qd-monitor` (Azure Automation, Python 3) | the Automation account's managed identity | read and start this VM; read its power state |

The observer has no Azure credential and no service-control permission. The
metadata service (where the VM's token is issued) is reachable only by uid 0
(`nftables/quantdesk-imds.nft`), and the observer's unit also denies it.

## The transaction

| State | What has been done |
|---|---|
| `ACCEPTED_REQUEST` | prior state of every unit and timer recorded; pre-checks: window, clock, holds, special sessions, package activity, one API process = MainPID, IMDS isolation, no open positions |
| `FROZEN` | active observer timers stopped (no new scheduled run); the observer's own lock taken and held (no manual run); the active automatic-update timers (`apt-daily`, `apt-daily-upgrade`) stopped - never disabled or masked - and package activity checked again after the pause. From here the admission gate refuses every start of the workload units in this boot except this transaction's own restoration |
| `EVIDENCE_CLEAR` | observer evidence, bound to this transaction, says CLEAR |
| `SYSTEM_CLEAR` | the controller's own facts re-established, API identity unchanged |
| `WEB_STOPPED` | dashboard stopped |
| `API_STOPPED` | API stopped with PostgreSQL and Redis still up: scheduler, imports and paper engine stop, writers drain; clean exit (status 0, not 70) required, and its absence CONFIRMED: PRESENT / ABSENT / UNKNOWN from systemd (inactive, no MainPID) and a plausible process table; only ABSENT continues |
| `WRITERS_EXCLUDED` | QuantDesk's writer key `(0x5144, 1)` taken EXCLUSIVE on the controller's session (its server process id recorded): proof no writer remains, and every supported write refuses itself while it is held |
| `POSITIONS_CLEAR` | open paper positions and open journal trades re-read under the key: zero; the key proved still held by the same session |
| `DRAINED_EVIDENCE_CLEAR` | observer evidence again, bound to the stopped API unit; key proved held |
| `REDIS_STOPPED` | stopped cleanly; key proved held |
| `POSTGRES_STOPPED` | the WAL insert position read under the key; the key's session kept in one statement through the cluster's stop; `pg_controldata` reports `shut down` AND its shutdown checkpoint lies exactly at that position (no WAL - no commit by anyone - after the last proof). The session's 57P01 ending is required but proves nothing about other sessions |
| `SUBMITTING` | recorded, and a status record kept, before the deallocation request leaves |
| `NOT_SENT` / `REJECTED` / `ACCEPTED` / `COMPLETED` / `UNKNOWN` | what is known of the request (below) |

Any failure or doubt before `SUBMITTING` restores the recorded prior state:
PostgreSQL, Redis, the API and the dashboard are started if they were running
before and are not now; the observer lock is released; then exactly the
observer timers that were active are started. `RESTORED` or `RESTORE_FAILED`
(retried at the next run). A pre-check failure is `ABORTED` with nothing
changed.

### Concurrency guarantees

- **One controller at a time:** a non-blocking coordination lock held for
  the whole run; a second run changes nothing and leaves requests in place.
- **No scheduled observer run** after `FROZEN` (timers stopped), **no manual
  one either** (the observer lock is held from `FROZEN` to the end and is
  passed to the evidence reader). A manual liveness run still sends its
  heartbeat and skips its housekeeping, which is its existing behaviour.
- **No new automatic update** after `FROZEN` (timers paused, activity checked
  after the pause); restored exactly by restoration, and started again by
  the next boot (they stay enabled). An arbitrary privileged manual command
  (`apt` run by hand) is not prevented and is not claimed to be.
- **No workload start** (API, dashboard, Redis, PostgreSQL) after `FROZEN`
  in this boot except the restoration: the admission gate. This bars the
  API - the process that hosts every scheduled writer - before its drain.
- **No supported writer** from `WRITERS_EXCLUDED` while the key's session
  is proved alive (same server process, key held) at every later stage.
- Positions, writers and pending observer work are checked again **after**
  the drain, under those locks, not by comparing snapshots.

### WRITER EXCLUSION: BLOCKED (not proven)

Unattended shutdown is **not** claimed safe. The required sequence and where
it stands:

1. *Exclude new supported writer admission* - only partly. The API (which
   hosts every scheduled writer) is barred from restarting by the admission
   gate from `FROZEN`. Standalone supported writers (`run_as_writer`,
   `doctor`) cannot be barred before the drain within the authorized scope:
   the only database-level bar is a queued EXCLUSIVE request on the key, and
   PostgreSQL then refuses every new `pg_try_advisory_xact_lock_shared` -
   including the API's own normal drain-time writes, which must not be
   disrupted. A standalone writer during the drain is therefore either
   finished before the key is taken (its effects are re-read under the key)
   or still holds its lease (the shutdown is abandoned): safe, but step 1 is
   not met.
2. *Let admitted work drain normally* - yes: the API's own drain, unchanged.
3. *Acquire exclusive coordination* - yes: the key, tried once, never queued.
4. *Recheck positions and evidence* - yes, under the key.
5. *Hold until PostgreSQL stopped accepting work and is confirmed stopped* -
   not proven. SQLSTATE 57P01 on the controller's session is **not** evidence
   that the cluster stopped admitting work: an individual
   `pg_terminate_backend` gives the same code, and in a fast shutdown other
   sessions are signalled independently of ours. The controller now requires
   the shutdown checkpoint to lie exactly at the WAL position read under the
   key (any later commit by anyone moves it), which would close the gap -
   but that PostgreSQL behaviour is UNVERIFIED on a real cluster (no
   disposable test machine was authorized), so it is not claimed.

### Azure outcomes

| Answer | State | Then |
|---|---|---|
| token or connection/TLS failure | `NOT_SENT` | restore |
| 400, 401, 403, 404, 409, 429 | `REJECTED` | restore |
| 200 | `COMPLETED` | done |
| 202 (`Azure-AsyncOperation` / `Location`, `Retry-After`) | `ACCEPTED` | poll the operation's own URL within budget, never earlier than `Retry-After` (as given; malformed: 300 s; absent: 15 s): Succeeded → `POLL_SUCCEEDED`; Failed/Canceled → `POLL_FAILED` → restore; otherwise `UNKNOWN_RETAINED` with the earliest permitted next read, which later runs honour |
| send failed part-way, no or unreadable response, 5xx | `UNKNOWN` | nothing restarted, nothing resubmitted |

Never inferred from elapsed time, a running VM or a new boot. A later boot is
recorded as `OBSERVED_NEW_BOOT` once and concludes nothing. An unresolved
transaction blocks every new shutdown **and every workload start** (the
admission gate, also at boot), and the Azure monitor reports it at every run,
deallocated or not, until operation-specific evidence or an operator's
recorded finding settles it:

```
power.py resolve --config /etc/quantdesk-power/power.json --txn <id> \
  --finding deallocation-completed|deallocation-not-performed|restoration-repaired \
  --evidence-ref "<Azure activity log entry or support reference>" --recorded-by <name>
```

`deallocation-not-performed` restores. `deallocation-completed` is accepted
only in a later boot than the request's (a completed deallocation ends that
boot); it, like a `POLL_SUCCEEDED` found by recovery in a later boot, then
starts the workload units that ran before the shutdown and that the gate held
down.

### Workload recovery is separate from Azure's outcome

`COMPLETED`, `POLL_SUCCEEDED` and `OPERATOR_CONFIRMED_DEALLOCATED` settle
Azure's side only. The transaction stays open until, in a later boot,
`RESUMING` is journaled **and read back intact** (before anything is
started; if that fails nothing is started), every unit and timer that ran
before the shutdown is started if needed and **verified ready**, and
`RESUMED` closes it;
otherwise `RESUME_FAILED`, retried by every recovery run (boot, hourly) until
the journal reserve, then an operator (`restoration-repaired`). Restoration
before the request left works the same way (`RESTORING` first, then
`RESTORED` / `RESTORE_FAILED`; if `RESTORING` cannot be written and read
back, restoration is **withheld**: nothing is started, the journal keeps what
it had, the run fails (`JOURNAL UNUSABLE`, exit 2), the status record shows
`IN_PROGRESS` (an alert), and recovery retries hourly). Per-unit progress of
both - started, already active, failed, verified, not running, unconfirmed -
is kept in a sealed, chained progress
log (`<state_root>/progress/<txn>/`). An interrupted attempt is followed by a
new one that starts from the units' real state. Nothing in restoration,
resumption or recovery sends a deallocation request.

**Readiness is a condition, not a delay.** After the starts, every unit is
read every 2 s until all are ready with an unchanged identity (main PID and
start time) for 15 s, or until the deadline (`timeouts.start_unit` + 15 s).
That is ONE absolute monotonic deadline for the whole assessment: each
inspection is refused when no time is left and otherwise waits no longer
than what is left, a reading that finishes at or after the deadline is not
ready, stability is timed from the end of the first complete all-ready
reading, and the clock is read again immediately before verification.
Ready means, for each service (PostgreSQL, Redis, the API, the dashboard):
systemd reports it `active` and `running` - for PostgreSQL and Redis
(`Type=notify`) that is the server's own readiness report - with a MainPID
and a main-process start time that are both present, decimal, nonzero and in
range; for the API also exactly one API process that is that MainPID, with
the same start time in the same reading. Missing, malformed, zero,
inconsistent or unreadable identity is never ready, so repeated missing
values are never taken for a stable identity. A timer has no process: an
active timer is ready. A unit not ready at the
deadline is `not_running`; one that was ready but restarted or never held
steady is `unconfirmed`; anything unreadable is not ready. All are failures
(`RESTORE_FAILED` / `RESUME_FAILED`, an alert flag), retried by recovery.
The API's application-level health (an HTTP health request) and Redis's
answer to a command are **not** checked: both need an interface the
controller does not have (PROPOSED, needs approval).

### The admission gate (boot and crash recovery)

`power.py gate` (drop-in `systemd/quantdesk-workload-10-power-gate.conf`)
refuses a workload start when: a transaction is Azure-unresolved; a
transaction is in a stopping stage (`FROZEN` .. `POSTGRES_STOPPED`) in this
boot; a transaction says deallocated in the boot that asked; more than one is
open; or the boot id, the state directory or the journal cannot be read or do
not validate. It reads only, writes nothing and takes no lock. A refused
start fails the unit visibly, the observer's assessments then fail and alert,
`power.py status` shows the reason, and the status record carries the flag
to the Azure monitor.

## Monitoring

- **Shutdown status** (`quantdesk_power/status.py`): a signed one-line record
  (VM digest, boot, sequence, transaction, state, submission boot, flag,
  operating cycle, time) kept at every controller run and before each
  request leaves. The cycle is the latest required startup - every weekday
  07:30 IST, holidays included, and each special session's VM start - and
  the monitor computes it independently: a record from an earlier cycle
  (a weekend's shutdown record after Monday's required startup) is an alert,
  because nothing shows that startup happened; weekend and overnight
  downtime stays quiet because no startup was required since; an unreadable
  special-session list exempts nothing. Records are never rewritten. The
  Azure monitor refuses a missing, malformed, forged, other-VM, future,
  replayed (older sequence), conflicting (same sequence, other content) or
  stale record; any UNRESOLVED / RESTORE_FAILED / RESUME_FAILED / CORRUPT /
  IN_PROGRESS flag alerts at every run, deallocated or not, whatever the
  hold tag says (IN_PROGRESS is never published by a run that finished: it
  is an unfinished restoration or resumption); a deallocated VM whose last record is not a
  submitted deallocation alerts. **Not yet carried to Azure** (see Limits).
- **Healthchecks.io** (`monitoring/healthchecks.json`): the four original
  identities. Liveness: OnCalendar `Mon..Fri 07:45/5` and `Mon..Fri
  08..16:00/5`, grace 10 min, so the first heartbeat is due 07:45 and alerts
  by 07:55, the last is expected 16:55, and nights and weekends are silent.
  Assessments unchanged: 08:35 + 10 min, 12:30 + 15 min, 16:00 + 30 min.
  A delivered `/fail` keeps its check down overnight; an undelivered current
  `/fail` blocks the shutdown, so the observer keeps retrying.
- **Azure, independent of the VM** (`azure/`): `qd-monitor` fails its job
  when the VM is not running 07:50–17:30 on a weekday (missed start or
  unexpected stop), is running at 21:30/01:00/07:15 or at the weekend without
  a hold (controller never ran, was blocked, or a deallocation is
  unresolved), or is stopped-but-allocated or mid-transition (incomplete
  shutdown). `qd-runbook-failed` emails on any failed `qd-start`/`qd-monitor`
  job (metric `TotalJob`, dimensions `Status` and `Runbook`),
  `qd-vm-operation-failed` on a failed start or deallocation at the Azure
  level. The same alert repeats at each scheduled run while the condition
  lasts. A monitor that never runs produces no metric samples, so no metric
  alert can see it: `qd-monitor-missing` is now a log search alert
  (`azure/alerts.monitor-missing.proposal.json`, fires on zero completed
  jobs in 12 hours), which needs a Log Analytics workspace and a diagnostic
  setting - **an approval blocker, not provisioned**.
- **Holds and missing sources.** A hold only ever keeps the VM *up*: the
  controller's KEEP_RUNNING hold blocks a shutdown, and the monitor's
  `qd-power-hold` tag excuses only a VM still running after hours. Neither
  touches restoration, resumption, the startup runbook, a daytime or
  special-session stop alert, or any status alert. A missing hold directory
  is UNKNOWN (never "no holds"), which blocks the shutdown and publishes no
  cycle (an alert). A missing `QD_SPECIAL_SESSIONS` variable is an alert (it
  must hold `[]` when there are none). Each entry needs `date` (a canonical
  `YYYY-MM-DD` string that round-trips; compact and ISO-week forms are
  refused, and no date may repeat), `vm_start`,
  `expect_running_from` and `expect_running_until`, timezone-aware, all
  three times on that date in IST (no schedule crossing midnight), and
  `vm_start < expect_running_from < expect_running_until`. Anything else fails the job as a `ConfigurationError` (still
  the alert) and exempts no downtime.
- **Special sessions:** `power.py plan-special --record FILE` turns one
  `quantdesk_special_session/1` record into a one-off start, explicit
  deadlines (VM running and first heartbeat by a time, alert 10 min later), a
  KEEP_RUNNING hold, monitor entries, and the OnCalendar lines an operator
  adds to the hosted liveness check by hand. The VM never pauses or edits a
  hosted check.

## Calendar

The accepted Phase 3B sessions (14, 15, 16, 19 and 21 Oct 2026), the 19 Oct
adjusted expiry, the 20 Oct holiday and the original boundary (21 Oct, 16:30
IST) are unchanged. The official F&O normal close is 15:40 IST from 3 Aug
2026 (NSE/FAOP/74467); QuantDesk's collection model and the observer's
`SESSION_CLOSE` stay at 15:30 and are not changed. The controller acts only
from 17:30, after both. A weekday is never assumed to be a session: an
unreviewed date requires a confirmed after-close report before any shutdown.
A reviewed special session today or tomorrow blocks, and so does any doubt
about one: no entry, an unreviewed or malformed entry, or a session type
other than NORMAL or CLOSED. No hold or controller setting can permit a
shutdown past it; only the reviewed calendar can. Missing assessments stay
MISSING.

## Permissions (future; none provisioned)

- `quantdesk-power`: system user, no shell, no home, no sudo, no groups. Its
  configuration `/etc/quantdesk-power-request/request.json` (root 0644, schema
  `quantdesk_power_request_config/1`: `request_dir` and `window`, nothing
  else); it cannot read `/etc/quantdesk-power`. It walks to its request
  directory with search-only descriptors (`O_PATH`), so it needs only search
  on `/run/quantdesk-power` (0711) and write + search on `requests/` (1730,
  group `quantdesk-power`): it can create its own request and can neither
  list nor read the directory.
- PostgreSQL role `quantdesk_power` (`provisioning/power-db-role.sql`):
  `SELECT (status)` on `paper_positions` and `trades`, read-only by default,
  one connection. Advisory-lock functions and `pg_stat_activity` need no grant.
- Azure: `azure/role-automation.json` (read, instanceView/read, start) for the
  Automation identity; `azure/role-vm-deallocate.json` (read, deallocate) for
  the VM's identity; both assignable only at this VM. Reading an operation's
  status needs `Microsoft.Compute/locations/operations/read` above VM scope
  (resource group per ARM's documentation, subscription per Update
  Manager's; unvalidated); it exceeds the approved scope and is **not
  granted**, so an unreadable status leaves the outcome unknown.
- **Proposals only - nothing granted or provisioned:**
  1. *Status publication (VM-specific).* Custom role for the VM's identity,
     assignable only at this VM: `Microsoft.Resources/tags/write` (and
     `Microsoft.Resources/tags/read`), used through the tags-at-scope API
     (PATCH `<vm>/providers/Microsoft.Resources/tags/default`, operation
     Merge). Broader effect: the identity could change **any** tag on this
     VM, including `qd-power-hold`, which the monitor honours - so the hold
     must move off the VM (an Automation variable) before this is granted.
     No resources, no cost. The HMAC key must be provisioned on both sides.
  2. *Missing-monitor detection.* A Log Analytics workspace, a diagnostic
     setting sending the Automation account's `JobLogs` to it, and the
     log search alert in `azure/alerts.monitor-missing.proposal.json`
     (hourly, 12 h window). Recurring cost: ingestion of a few KB per job
     (about 40 jobs a week) and the alert rule's monthly charge; both small,
     to be priced for the region before approval. The rule needs read on the
     workspace only.
  3. *Async-operation status reads.* `Microsoft.Compute/locations/operations/read`
     for the VM's identity. The operation URL is not scoped to the VM
     (`/subscriptions/<id>/providers/Microsoft.Compute/locations/<loc>/
     operations/<id>`). ARM's documentation says resource-group permission
     suffices and resource-level does not; Update Manager's lists
     subscription scope. Ask for the resource group first, after an
     isolated test confirms it; subscription only if that test refuses it.
     Broader effect: status (times, error codes; no resource data) of
     Compute operations in that resource group (or subscription). No cost.
     Without it every accepted deallocation stays UNKNOWN and needs a daily
     operator finding.
- Files: `/opt/quantdesk-power` root 0755; `/etc/quantdesk-power` root
  0750 (`power.json`, `pg_service.conf` 0640; `pgpass` 0600 owner root;
  `holds/` root 0700); `/var/lib/quantdesk-power` root 0700 (journal,
  consumed nonces, refusals); `/run/quantdesk-power/requests` 1730
  root:quantdesk-power.

## Installation (not done; each step needs approval)

1. Observer v0.4.14 reviewed, installed and accepted as a separate step.
2. Accounts, DB role, files, `/etc/tmpfiles.d/quantdesk-power.conf`,
   the observer drop-in `20-power-isolation.conf`.
3. Units copied, `daemon-reload`; `quantdesk-power-imds.service` enabled and
   started; `nft -j list table inet quantdesk_imds` checked by the
   controller's structural rule check, and a non-root request to
   169.254.169.254 confirmed refused. `power.py status` run once (creates the
   state directory and journal), then the gate drop-in installed for the
   four workload units, and a start of each confirmed allowed.
4. Azure: custom roles, the VM identity, the Automation account, runbooks,
   variables (`QD_VM_RESOURCE_ID`, `QD_SPECIAL_SESSIONS`), alerts. Action-group
   test email **received and confirmed**. Hosted checks configured; hosted
   test email **received and confirmed**.
5. Schedules enabled: `qd-monitor` first, then `qd-start`, then the request
   timer and path unit last.

## Rollback

1. Disable the request timer and the path unit (no new transactions).
   The gate drop-ins stay until step 2 is complete: they are what keeps the
   workload down while a deallocation is unresolved.
2. `power.py recover`, then `power.py status`: **no unresolved transaction
   may remain** before anything else is removed. An unresolved one is
   settled first (`resolve`), never discarded.
3. Disable the Azure `qd-start` schedule (the VM is then started by hand).
4. Keep `qd-monitor` and the alerts until the VM's state is settled, then
   remove them; remove the role assignments and roles **this installation
   created** (a pre-existing identity or role is never disabled).
5. Remove the gate drop-ins (`daemon-reload`), then units, nftables table,
   tmpfiles entry, accounts and the DB role.
   `/var/lib/quantdesk-power` is archived, not deleted.

The observer's timers are never disabled by any step; the controller only
stops them for the duration of a transaction and restores them exactly.

## Limits

- Root, and anyone who can become root (azureuser has sudo), is not
  constrained by the IMDS rule, the gate, or the update pause.
- A manual observer liveness run during a shutdown still sends its heartbeat.
- **Every accepted deallocation stays UNKNOWN on this machine** while the
  VM's identity cannot read the operation's status
  (`Microsoft.Compute/locations/operations/read`, resource-group or
  subscription scope, not granted): the controller is ended by the
  shutdown before Azure's answer is final, and at the next boot the gate holds the workload down and the
  monitor alerts until an operator records `deallocation-completed`. That
  permission, or the daily finding, is a decision for approval.
- The status record is not carried to Azure (tag permission and key not
  approved); until it is, `qd-monitor` fails every run on the missing status.
- `qd-monitor-missing` needs a Log Analytics workspace (not approved).
- The hosted reminder setting and plan limits are verified at acceptance.
- WRITER EXCLUSION: BLOCKED (above).
- Untested against real systemd, PostgreSQL, nftables, Azure or
  Healthchecks.io; no disposable Linux machine was authorized. The tests that
  would exercise the requester as a real second account, the systemd
  sandbox and nftables run only on Linux as root and are skipped elsewhere.
