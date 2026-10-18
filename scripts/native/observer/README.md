# QuantDesk operational observer

A separate, read-only program that checks the desk at fixed times and writes a
report each time. It is **prepared and tested locally only**. Nothing here is
installed on the VM, no account or credential exists for it, no Healthchecks.io
check is provisioned, and no Telegram or email destination is connected.

It is not part of QuantDesk. It has its own version (`VERSION`), imports no
application code, and is meant to live at `/opt/quantdesk-observer`, outside
the production checkout. Synthetic tests do not establish production
readiness.

## What it does

| Run | Scheduled start (Asia/Kolkata) | Completion deadline | Purpose |
|---|---|---|---|
| `premarket` | 08:35, Mon–Fri | 08:45 | Readiness before the session |
| `midsession` | 12:30, Mon–Fri | 12:45 | Collection progress and service state |
| `afterclose` | 16:00, Mon–Fri | 16:30 | Session qualification and checkpoint detection |
| `liveness` | every 5 minutes, every day | — | A ping only: the observer ran and could reach out |

Each run is a short process started by a systemd timer. It takes one lock (so
runs cannot overlap), limits its own running time — including when it starts
late — publishes one new report and exits. A fault in the observer ends as a
recorded failure in its own directory and affects nothing in QuantDesk.

**Assessment times are strict.**

- There is no early-start allowance. A run that starts before its scheduled
  time checks nothing and cannot satisfy the assessment.
- A run is timely only if it has *finished* by its deadline. The deadline
  second itself is in time; one second later is not.
- A late run still records what it finds, with its lateness. A late
  after-close run can establish complete collection, and the session then
  needs an operational exception.
- Each report keeps the scheduled start, the actual start, the completion
  time and the deadline separately. Each notification record adds the
  notification time. No later run changes an earlier report.

**Waiting for the lock.** The liveness run fires in the same second as each
assessment (08:35, 12:30, 16:00) and may hold the lock for a few seconds. An
assessment therefore tries for the lock at most once a second:

- inside its window, only while time remains before its completion deadline:
  never at the deadline itself or after it;
- when started late (or as a reconstruction), only while time remains of its
  one 180-second allowance, which covers the wait and the work together;
- when started early, once only; it cannot wait its way into the window.

The wait is the assessment's own time: its running limit is recomputed once
the lock is held, and no deadline moves. The time left is checked before each
attempt for the lock and again immediately before the work starts. If none is
left (zero or less) nothing starts: the report says `assessment_status:
MISSING` with `deadline_exhausted`, and no fresh 180-second allowance is ever
given in its place. A run that starts exactly at its deadline therefore does
nothing. Work cut off by its limit is recorded as interrupted
(`assessment_interrupted_at`), never as completed or timely. If the lock is held throughout, the
report says `assessment_status: MISSING`, records the contention (attempts,
last attempt, limit) and checks nothing; it is never a pass. Reports record
the invocation, the last lock attempt, the lock's actual acquisition (read
only after the lock is held), the assessment start and the assessment
completion as separate times. A lock that is held only at or after the limit
leaves no time, so nothing starts. Liveness never waits for the lock.

**An unexpected error in the phase calculation.** The checks already made stay
on the report. The bounded phase is `UNKNOWN`; an after-close outcome is
`UNKNOWN` with `qualification_withheld`, so the session is not counted and no
new early-review trigger is accepted. An existing checkpoint is untouched. The
failure report is still published, an observer fault and its after-close
`/fail` intent are recorded, the run exits non-zero and the lock is released.
Only the exception's type is reported; the local fault summary is redacted.
The fault resolves, without a recovery signal, once a later invocation works
the phase out again.

**Publishing a report, and confirming it.** A report counts as evidence only
once it is positively confirmed. Publishing is, in order:

1. publish the immutable report (written, synced, linked to its final name);
2. sync it, its directory and every directory above it up to the records
   root (a new session-date folder is itself a name in its parent);
3. only then publish a confirmation, `confirmations/<date>/<report name>`,
   binding the report's id, relative path, content hash and file hash;
4. sync the confirmation and the directories above it likewise.

`Store.reports()` returns only confirmed reports, and it is the reader that
every decision uses: pre-market readiness, the 12:30 completion, session
qualification, the baseline, the early-review trigger, the checkpoint,
unreported-activity days and notification supersession. A report is confirmed
only when its confirmation has been read, matched to the report exactly (id,
path, content hash, file hash) and synced together with the report — and this
is done again on every authoritative read, from the files as they are then.
No positive verdict is remembered, so a report or confirmation changed,
removed or substituted after an earlier read is rejected at once, by the same
process exactly as by a new one; a file that survived a crash is never taken
on trust. The one thing a process does remember is its own failures: a
confirmation or resolution it tried to make and could not stays failed for
the rest of that process, however its file reads afterwards; a later process
may verify and sync it under these rules. A readable report, a
valid seal or the absence of a fault is not confirmation, and a report with
an open publication fault is not confirmed whatever its confirmation says.
Reports with no confirmation — including any written before 0.4.3 — stay on
disk for diagnosis (`all_reports()`) and count for nothing; nothing certifies
them from their own contents.

**A report that cannot be published or confirmed.** A failure at any of those
steps is classified from what the disk then holds under the report's name:

- `ABSENT`: nothing is there, and the failure came before the final link;
- `PRESENT_UNCONFIRMED`: this exact report (its id and content hash) is there,
  but syncing it, or publishing or syncing its confirmation, failed;
- `UNKNOWN`: anything else. A failure after the final link is never called
  absent.

The report's bytes are left exactly as they are: never deleted, rewritten or
re-published. A `report-publication` fault (reason
`observer_report_publication_failure`) is recorded with the report's id,
path, content hash, file hash and the failed step, and with what it means in
three parts (`effect`): the publication barrier (the report counts for
nothing until confirmed), the missing evidence (for an `ABSENT` report, the
assessment stays MISSING), and the monitoring attention (until the recovery
below). Its after-close `/fail` intent is delivered within the invocation's
usual reservations, request limits, spacing and budget. No assessment signal
is sent for that report, the run prints `REPORT <state>` and exits non-zero,
and the lock is released. If the fault cannot be stored either, the run says
`STORAGE UNAVAILABLE`, exits non-zero, and writes and sends nothing more; the
report still has no confirmation, so it still counts for nothing.

**Recovering a present report.** Only a later invocation, after its
housekeeping has reconciled its state, and only by: finding that exact report
under its recorded name with the id and both hashes its fault recorded (the
fault record is the independent evidence); syncing it and the directories
above it; publishing and syncing its confirmation; and publishing and syncing
the resolution. The phase is then accounted again, so the report counts as
what it says — no more: a late, interrupted or reconstructed assessment stays
exactly that. If any step fails, the fault stays open and the report stays
excluded. A resolution is recognised — on every read, in any process — only
when it agrees exactly with its fault, the report and the report's
confirmation: the fault id, the report id, the report's relative path, the
content hash and the file hash (`verified_file_sha256` must equal both the
hash the fault recorded and the bytes on disk), every hash well-formed and
present, and the confirmation matching; and only after it has been synced.
The same check is applied before a resolution is written. A fault that
recorded no file hash cannot be resolved this way: nothing missing is
inferred. One that is unreadable, names something else, disagrees in any of
these or cannot be synced leaves the fault open (`resolution_rejected`). File existence, other successful work, housekeeping,
liveness and acknowledgments resolve nothing.

**Recovering from an absent report.** The missing assessment stays MISSING
for good. Only the monitoring attention ends, and only through a genuine
later scheduled assessment of the same check: not a reconstruction, a replay
of another day or an early run; its checks run to completion, not
interrupted; a different invocation from the failed one; started strictly
after the fault was detected, completed no earlier than it started and no
later than the resolution — all times compared as instants, and anything
missing, malformed or contradictory rejected. Its report must be published
and confirmed with no publication fault of its own, the missing report found
still absent (no report and no confirmation under its name or id), and a
resolution linking the fault, the missing report and the replacement (ids,
paths and both hashes) published and synced. Every one of these conditions
is checked before the resolution is written and again whenever it is read,
in any process. The replacement's result need not be PASS, and it may be late
under the existing late-run rule: it shows that publication works again.
Another check, liveness, an acknowledgment, a reconstruction, an early or
interrupted run or an unconfirmed replacement does not resolve it. Resolution of either kind changes no report, supplies no missing
readiness, qualifies no earlier session, approves no exception, reopens no
checkpoint and sends no recovery.

## What it can and cannot reach

Its whole reach is three allowlists in `quantdesk_observer/probes.py`:

- **Programs:** `git`, `systemctl show`, `ps`, `ss`, `timedatectl`, `psql`.
- **Files:** QuantDesk's backend log and its numbered or `.gz` rotations up
  to `.14` (for the scheduler's own start and run times), and the
  unattended-upgrades log, all read only.
- **API:** `GET` on `/health`, `/health/scheduler`, `/health/feed` and
  `/v2/status`, only at plain `http://` on a literal loopback address. A
  hostname is refused. Redirects are refused, not followed. No proxy is used.
  A reply over 1 MiB, or one that is not a JSON object, is refused.
- **Database:** seven `SELECT` statements, each in a session forced to
  `default_transaction_read_only=on` with a 15-second statement timeout.

It never calls `/signals/live`, never asks for market data, never starts,
stops or restarts a unit, never reboots, never writes to QuantDesk's database
or checkout, and never repairs anything. `git status` runs with
`GIT_OPTIONAL_LOCKS=0` so it does not rewrite the checkout's index.

## Qualification policy

These are Astra's decisions. The seven states are not interchangeable.

| State | Applies to | Meaning |
|---|---|---|
| `QUALIFIED` | a session | Every evidence gate passed and every operational requirement was established. |
| `INCOMPLETE` | a session | An evidence gate failed or could not be established. The session is preserved and does not count. |
| `OPERATIONAL EXCEPTION REQUIRED` | a session | Every evidence gate passed, but an operational requirement failed, was missing or could not be established. It counts only with an explicit operator approval record. |
| `REVIEW PENDING` | the bounded phase | A checkpoint was reached. Final once recorded; the observer cannot reopen the phase. |
| `UNKNOWN` | a single fact, or a date | Existing evidence cannot establish it. Never treated as success. |
| `MISSING` | the 08:45 or 12:30 assessment | No timely assessment happened. Never reconstructed as `PASS`. |
| `NO SESSION` | a date | A reviewed calendar lists the date as closed, the reads succeeded, and no activity exists. |

**Evidence gates** — any failure or unknown gives `INCOMPLETE`:

| Gate | Rule |
|---|---|
| Collection | All 75 scheduled observations, none missing, duplicated, off schedule or outside the session, against the schedule described under "Observation schedule". If that schedule cannot be established the gate is `UNKNOWN`. |
| Producer | Every observation carries the expected `code_id`, commit and clean worktree, and a unique observation id. |
| Configuration | `parameter_hash`, strategy version and `v2_config_hash` equal the accepted baseline. |
| Linkage | One decision per observation whose evidence is structurally valid `v2_decision_evidence/2` (see "Decision evidence"), labelled complete, linked, with matching ids, action, code id and parameter hash. |
| Gate traces | Every decision's trace follows the producer's contract (see "Gate traces and rejection codes"). |
| Option chain | On all 75 observations: the check enabled, at weight 0.16, with OI available. A genuinely unavailable chain is listed with its stored reason and still fails the gate. A neutral contribution with valid OI passes. |
| Score | Confidence equals \|Σ score×weight ÷ Σ enabled weight\| within 5.1e-5 (four-decimal rounding); weights sum to 1; HOLD below the stored threshold, otherwise BUY for a positive signed score and SELL for a negative one; every input finite. |
| Archive | Every archive row has an exchange time, none later than its capture, all dated to the session. |
| VIX close | A row for the session date with a valid value and the source that QuantDesk's close collection writes, stored after the close on the session date. Otherwise `UNKNOWN`. No storage-time window applies; a late write passes. The market observation time is not stored and is reported as `UNKNOWN`. |
| Paper activity | Every position opened in the session belongs to an `entered` decision and the reverse; counts match the last trusted baseline; the journal is unchanged. Built from the one session snapshot (see "One evidence snapshot"). An unreadable source is `UNKNOWN` and sets no baseline. |
| Live trading | The API reports `live_trading: false` (when observed at the time). |

**Operational requirements** — any failure, miss or unknown gives
`OPERATIONAL EXCEPTION REQUIRED` when the evidence gates passed:

| Requirement | Rule |
|---|---|
| 08:45 readiness | `PASS` needs a run that started in 08:35–08:45, passed every required check and finished by 08:45:00. Started in the window but failed or finished late: `FAIL`. No run started in the window: `MISSING`. |
| After-close timeliness | The after-close run started at or after 16:00 and finished by 16:30. Otherwise a session whose gates all passed is `OPERATIONAL EXCEPTION REQUIRED`. |
| Operational continuity | Two counter snapshots of the same API process (same PID, counting start and feed start), one before the open and one after the close, with every required counter present and none changed. A missing job or counter, a reset, a restart, or any change the counters cannot place in time is `UNKNOWN`. Lifetime totals and reconnects are recorded, not judged. |
| Single runtime | One API initialization across the session's observations. A restart is recorded with its times and needs review; it is not by itself a collection failure. |
| Mid-session facts | Established only by a timely passing 12:30 run. If that run is `MISSING` it stays `MISSING` and the facts are `UNKNOWN`: unchanged counters and a single API start do not show that PostgreSQL, Redis, the scheduler, the Angel feed and the option stream were healthy at 12:30, so they do not replace the assessment. A 12:30 run that failed is not overridden. |
| Session date | The date's own calendar entry is reviewed and lists a normal session. |

Other approved rules:

- **Early trigger and readiness are separate.** A verified natural
  `expiry_day` pass in a session whose evidence gates passed sets `REVIEW
  PENDING` even if readiness was `FAIL` or `MISSING`. That session still does
  not count without an approval.
- **Reboot-required** is recorded as maintenance information and is not a
  failure. The observer never reboots.
- **Restarts** are recorded with the times systemd reports. `NRestarts` counts
  automatic restarts only and does not prove a complete history.
- **Reconstruction.** `afterclose --session-date` rebuilds counts, linkage,
  identity, gate outcomes and paper activity for an earlier date. Process,
  feed and live-trading state were not observed then, so they are `UNKNOWN`
  and the outcome is at best `OPERATIONAL EXCEPTION REQUIRED`.
- **No observations on a date of unknown status** gives outcome `UNKNOWN`:
  neither an incomplete session nor a holiday is claimed.

## Observation schedule

The expected slots come from QuantDesk's own scheduler log. They are never
taken from the API's start time, the stored observations, another interval
job or the market open.

**What the log can and cannot say.** For every run it starts, APScheduler
logs the time that run was scheduled for:

```
… INFO apscheduler.executors.default: Running job "tick (trigger: interval[0:05:00], …)"
(scheduled at 2026-10-13 09:18:26.692409+05:30)
```

That line names the job by its function name and trigger. It does not carry
the job id `nifty-agent`, and all three QuantDesk jobs run a function called
`tick`. So a five-minute run line, alone, does not say whose run it was. A
matching interval is not accepted as identity.

**What identifies the agent.** A five-minute `tick` line is attributed to the
signal agent only when the same scheduler start shows all of these, each
written by the named logger at the frozen revision `2d1d25a71394`:

| Evidence | Logged by | Requirement |
|---|---|---|
| The scheduler's start | `apscheduler.scheduler` | retained in the log, before the runs |
| `Nifty agent scheduled every N minutes.` | `app.workers.agent` | exactly once, with N = 5 |
| `price ticker running every Ns` | `app.workers.ticker` | exactly once, not 300 s |
| `option chain collector running every Ns` | `app.workers.option_collector` | exactly once and not 300 s, or its "disabled" warning |
| `Added job "tick" to job store` | `apscheduler.scheduler` | exactly the jobs the frozen application registers (three, or two with the collector disabled) |
| Every `Running job` line | `apscheduler.executors.default` | job name `tick`, on one of those declared intervals |

The words of a declaration inside another logger's message do not count.

**What establishes the schedule.** With the agent identified, its logged run
times for the session date must all sit on one five-minute grid from one
scheduler start, and every slot that is due must itself be a logged
scheduled time. That grid across 09:15–15:30 gives the 75 slots.

The schedule is `UNKNOWN`, the collection gate is `UNKNOWN` and the session
is `INCOMPLETE` when any of these holds:

- an unrelated job runs on the same interval, or a job the frozen application
  does not define is registered or runs;
- the agent's declaration is missing, repeated, unreadable or not five minutes;
- another QuantDesk job declares the same five-minute interval;
- the log is unreadable, or does not retain the scheduler's start;
- no run time is logged for the date;
- a due slot is not in the log (a gap, lost lines or a skipped run);
- the times come from two scheduler starts or two grids;
- the start that logged the runs is not the running API process's own (below).

Nothing is invented and a missing slot is not filled in from its neighbours.
The 60-second tolerance only matches one observation to one logged slot. It
does not merge duplicates, excuse a missing slot, change a timestamp, move
the schedule or affect assessment start times.

**Where the start is read from (v0.4.9, corrected in v0.4.10).** QuantDesk
writes the scheduler's
start lines only when the API starts, and logrotate (daily, `rotate 14`,
`compress`, `delaycompress`, `copytruncate`) moves them out of `backend.log`
at the first midnight. So the observer reads `backend.log` and its rotations
`backend.log.1` … `backend.log.14`, each either plain or `.gz`, and nothing
else in that directory. The following are errors:

- both forms of one number at once;
- a symlink;
- anything other than a regular file.

The set ends at the first missing number.

**Path containment (v0.4.10).** `quantdesk_log` must be an absolute path
without `..`. Its directory is opened from `/` one component at a time, each
with `O_DIRECTORY` and `O_NOFOLLOW` relative to the one before. A symlink
anywhere on the way is therefore refused, not only one at the end. The files
are opened only by name inside that held directory, also with `O_NOFOLLOW`.

After the read, the path is walked again from `/`. Before anything is
confirmed:

- every directory on the way must still be the same (device and inode);
- every file read must still be under its name.

So a directory replaced during the read, even by one holding hard links to
the same files, makes the schedule `UNKNOWN`.

The files are opened once and held open, newest first. The observer reads
back only as far as the first file whose first timestamp is before the API
process started, then parses those files oldest first.

Which start counts is decided from systemd, not from the log. Before and
after the log read, the observer reads:

- the API unit's `MainPID` and `ExecMainStartTimestamp`;
- the processes whose command line contains `api_process_match`.

Exactly one such process is required, and its PID must equal systemd's
`MainPID` (v0.4.11). This is checked before any log file is opened; if it
fails, nothing is opened. After the read, both answers are read again. They
must still agree with each other and must be exactly the same as before. A
change in either, or in both together, makes the schedule `UNKNOWN`. The
observer never falls back to an earlier start.

systemd shows the start time T to the second, so the process began somewhere
in [T, T+1 s) (v0.4.10). A scheduler start is admitted as `current` only if
its first line is logged in [T+1 s, T+60 s): exactly T+1 s is admitted and
exactly T+60 s is not. On the Azure VM on 16 Oct the gap was about
10 seconds. This is a conservative admission rule, not proof that the
process wrote the line, and the 60 seconds is not widened automatically.
Every other start is classed as follows, and none of them establishes a
schedule:

- `within_start_second`: logged in [T, T+1 s), so it could predate the
  process;
- `earlier`: logged before T;
- `later`: logged at T+60 s or after;
- `ambiguous`: two or more starts anywhere in [T, T+60 s);
- `unattributed`: no readable time, or no start retained;
- `contradicted` (v0.4.13): it would be `current`, but the log also holds
  another start at or after T, or one with no readable time. A process
  starts its scheduler once, so the other start contradicts it, whether or
  not it ran anything on the session date. Starts before T belong to an
  earlier process and contradict nothing.

**The log's zone and writer (v0.4.10).** The log's timestamps carry no zone,
and its lines carry no writer. Neither can be read safely by the observer:

- the service environment can override the host's zone;
- systemd's display zone says nothing about the application's zone;
- nothing the observer may read shows which processes write the file.

So no start is attributed unless `observer.json` holds a `backend_log_basis`
record, written after a verification made outside the observer:

```json
"backend_log_basis": {
  "timezone": "Asia/Kolkata",
  "timezone_basis": "<how the API process's logging zone was verified>",
  "single_writer": true,
  "single_writer_basis": "<how it was verified that only that process writes the log>",
  "verified_by": "<who>",
  "verified_at": "<ISO time with zone>"
}
```

Only `Asia/Kolkata` (+05:30, no daylight saving) is accepted. Without a
complete record the schedule is `UNKNOWN`. `observer.json.example` does not
include the record: the verification it states has not been made.

The established schedule records the start's file, logged time, unit, PID,
start time, admission interval and the log basis.

The read is bounded:

| Limit | Value |
|---|---|
| Files | 15 |
| Decompressed bytes per file | 64 MiB |
| Decompressed bytes in all | 256 MiB |
| `.gz` size on disk | 16 MiB |
| Seconds | 20 |
| Line length | 64 KiB; a longer line is dropped whole, never split |

Exceeding a limit, a corrupt or truncated `.gz`, or a missing current file
makes the schedule `UNKNOWN`. So does any sign of rotation during the read:

- a name now pointing to another file;
- a rotated file changing size;
- `backend.log` shrinking;
- two files overlapping in time (a copy not yet truncated);
- the API process or the number of API processes changing;
- the directory or a parent directory being replaced or turned into a link.

**Kept start-up evidence (v0.4.12).** No schedule, slot, result or PASS is
kept between runs. What is kept is the *original evidence*: the exact bytes
of the running process's start-up declarations. These are its first
scheduler line, the `Scheduler started` and `Added job` lines, and the agent,
ticker and collector declarations, in log order. Each record carries:

- each line's file, device, inode, offset and length;
- the SHA-256 of the bytes;
- the time the start was logged;
- the run and observer version that captured it.

**Binding.** Each record is bound to:

- the boot (`/proc/sys/kernel/random/boot_id`);
- the API unit;
- `MainPID`, which must equal the one matching API process;
- the displayed and the monotonic start time
  (`ExecMainStartTimestampMonotonic`);
- the complete `backend_log_basis`.

**Where it is stored.** Records are filed as
`startup/<SHA-256 of the binding>/<SHA-256 of the bytes>.json` in the
observer's own state, through append-only publication: a synced scratch
file, then a link that never overwrites.

v0.4.13 tightened the storage:

- The state root is reached from `/` one component at a time without
  following any link. The root, `startup/`, each binding directory and each
  record must be owned by the observer's user and not writable by group or
  others.
- Every read, the scratch file, the link, its removal and the directory
  syncs are done relative to those held directory descriptors.
- Before a publication counts, the path is walked again from `/`, and every
  directory must be the same one. A replacement makes the schedule
  `UNKNOWN`.

**One decision at a time (v0.4.13).** For each binding, all of these happen
under one non-blocking `flock` on `.lock` in the binding's directory: loading
its records, reading the logs, detecting a conflict, publishing, and a final
fresh re-read of what is stored. If another run holds the lock, the schedule
is `UNKNOWN` at once, without waiting or retrying. Nothing read before the
lock is used. Before a decision stands, the stored records must hold nothing
but the evidence it used. So a run that sees different evidence for the same
process fails closed itself, not only the runs after it. The lock is
released when the run ends, even if its process is killed. A conflict
stored after a decision has been completed affects later inspections, never
the completed one.

**How it is used.**

1. Before any log is read, the boot, systemd and process evidence is read
   together, and the binding is formed from it.
2. That binding's records are then read. Each must pass all of these checks
   before anything is parsed, and any failure makes the schedule `UNKNOWN`:
   - the directories and file are opened without following links;
   - each is owned by the observer's user and not writable by others;
   - the size is bounded;
   - the record's own content hash, schema, file name and binding hash
     match;
   - the record has exactly the capture fields (v0.4.13). A record that
     lost a field and was resealed still fails;
   - `captured_at_utc` has a timezone;
   - `captured_by_run` is a run identifier;
   - `observer_version` is between 0.4.12 and this version;
   - the binding is complete and is exactly the running one;
   - every line's source is the backend log or one of its supported
     rotations, in log order, with consistent inodes and non-overlapping
     offsets;
   - the lines are valid base64 and complete;
   - the bytes hash to the recorded hash;
   - the lines form exactly one start's declarations.
3. After the log read, the boot, systemd and process evidence is read again
   and must be unchanged. Nothing is published before that check passes.
4. If the retained log holds any scheduler start at or after the process
   start, the log decides:
   - exactly one `current` start is captured the first time it is seen;
   - the same bytes seen again add nothing, even from another rotation file
     or inode;
   - different bytes for the same binding are kept as well, and that run
     and every later one for that process is `UNKNOWN`. Nothing is
     overwritten, deleted or chosen.
5. If the retained log holds no such start, the one kept record for the
   exact binding stands in for it. Run lines are never kept: every slot must
   still be a run line in the retained log. Kept bytes are never combined
   with other declaration lines; a retained declaration makes the schedule
   `UNKNOWN`. So do two different records, or any damaged one.

**Limits.**

- A restart, PID reuse (a new monotonic start), a reboot or a changed log
  basis gives a new binding. The old records are kept but never used.
- Only the midsession (12:30) and after-close (16:00) assessments read the
  log, so only they can capture a start, and only while it is still
  retained. The pre-market assessment and liveness never capture. Fourteen
  rotations need not mean fourteen days. If the start
  rotates away first, every session is `UNKNOWN` until the API next starts.
  The observer never restarts it.
- The hashes give internal consistency, not authenticity. Anyone able to
  write the observer's state could forge a record. Nothing signs records.

**Not verified on Azure.** The line formats above were reproduced locally
with the same APScheduler release (3.11.0) and read from the frozen source.
The backend log has not been read on the Azure VM for this purpose. Whether
it holds every one of these lines, at INFO, for a whole session, is unknown.
If it does not, every session's schedule is `UNKNOWN`. The tests use
synthetic lines; they show what the parser accepts and nothing about the
live log.

## Gate traces and rejection codes

`checks.GATE_ORDER` and `checks.GATE_CODES` restate the producer's contract —
`GATE_CODES` in `strategy_v2/evidence.py` and the code constants in
`strategy_v2/rules.py` at revision `2d1d25a71394` — and nothing else. A trace
is accepted only when:

- it lists every gate exactly once, in the producer's order;
- each gate's `enabled` and `status` agree (`disabled` only when not enabled);
- it passes up to at most one failure and reaches nothing after it;
- only the failed gate carries a code;
- for a rejection, the failed gate's code equals the decision's code **and is
  one that gate can produce**;
- for an entry, no gate failed or was left unreached, the code is `entered`,
  and a position id is present;
- the trace's own outcome and code equal the decision's.

So a decision coded `entry_state_not_ready` whose trace shows `expiry_day`
passed and `feed` failed is refused: `feed` cannot produce that code.

## Decision evidence

Checked against the envelope the producer's `EvidenceBuilder` writes:

- **Envelope:** `schema`, `status`, `issues`, `vector`, `contract_evidence`,
  `outcome_label`, `diagnostics`, `bases`, `missing`, `missing_scope`.
- **Vector sections** and their fields: `identity`, `causality`,
  `market_state`, `setup`, `evidence` (each check with its six fields),
  `data_status`, `versions` (including `api_init_provenance`),
  `decision_context` (including `gate_trace`).
- **`contract_evidence`** in one of the producer's two shapes.

Four cases are kept apart:

| Case | Example | Treatment |
|---|---|---|
| A. Present, legitimately unavailable | `market_state.vwap: null` | Structurally valid |
| B. `selection_not_reached` | a decision that stopped before the selector | Structurally valid |
| C. Missing required evidence | no `data_status` section | Not complete evidence; the session is `INCOMPLETE` |
| D. Contradictory or mislabelled | `status: complete` with issues listed, or contract evidence that disagrees with the trace | Not complete evidence; the session is `INCOMPLETE` |

**Selector consistency.** When the selector is reached the producer re-runs
it and stores `trace_consistent_with_decision`. The observer applies the
producer's own rules for it (`EvidenceBuilder.build` and `status_for`):

| Stored value | Producer's rule | Treatment |
|---|---|---|
| `true` | — | Valid |
| `false` | files a `selector_trace_disagreement` issue; status is `inconsistent` at best | Always contradictory. The session cannot be `QUALIFIED` and the decision cannot be an accepted early-review trigger, however the record is labelled. |
| `null` | files a capture error; status is `capture_incomplete` | Legitimately unavailable beside that capture error. Contradictory without it, or under `complete`. |
| not reached | no selector evidence | Valid; nothing to contradict |

The envelope's `status` must also be the one the producer's rule gives for
the issues it lists and its linkage. A null value is never rejected merely
for being null.

`status = "complete"` is never enough by itself. Structural validity is a
separate question from whether a legitimately unknown input blocks
qualification: a null field is valid evidence, and the gates above decide
what it means.

## One evidence snapshot

Each run reads the session's stored evidence once — observations, decisions,
positions, journal count, the scheduler's logged times and the paper engine's
position — and builds everything from that one snapshot: the gates, the
report fields, the `UNKNOWN` list, the paper reconciliation and the next
baseline. Nothing is read a second time and nothing is retried, so the report
cannot say `QUALIFIED` beside `paper_positions: UNKNOWN`. The report's
`evidence_reads` lists every source with `ok` or `failed` and the reason.

## The expiry_day trigger

Read from `paper_decisions.detail.evidence.vector.decision_context.gate_trace`.
A decision qualifies only when all of these hold:

- the trace is internally consistent (see "Gate traces" above);
- `gate = expiry_day`, `enabled = true`, `status = passed`;
- the evidence is complete `v2_decision_evidence/2` linked to a session
  observation;
- the observation is from the expected producer and configuration.

A decision rejected as `entry_state_not_ready` cannot also have passed
`expiry_day`: its trace must stop at the entry-state gate. A trace that says
otherwise, repeats a gate, or is incomplete is reported with the reason and
never accepted.

For each candidate the after-close report records the downstream stages
(feed, spot, levels versus spot, VIX, expiry choice, chain, contract
selection, lot size, premium risk, risk manager, paper position,
submission/fill) as `PASSED`, `REJECTED`, `NOT REACHED` or `UNKNOWN`. It copies
QuantDesk's stored `contract_evidence` unchanged: quote universe, bid and ask,
source timestamps where stored, rejected alternatives, the selected contract,
the selector's result and its consistency flag, and sizing levels. Nothing is
inferred. A selector attempt does not advance the Angel bid-parser status.

## Bounded observation phase

The phase began after 13 Oct 2026 and does not restart when the observer is
installed. Review is due at whichever comes first:

| Reason | When |
|---|---|
| `EARLY_REVIEW` | after the close of the first complete session with a verified natural `expiry_day` pass |
| `FIVE_SESSION_CHECKPOINT` | when five sessions in the window qualify |
| `BOUNDED_WINDOW_ENDED_WITH_UNRESOLVED_EVIDENCE` | at 16:30 IST on the fifth reviewed normal session after 13 Oct, whatever reports exist |

The window ends on time even if the final report is missing, a session did
not qualify, the observer was down, or fewer than five qualified. The
checkpoint then lists the missing reports, the unresolved qualifications, the
count that did qualify, the time the checkpoint was due and the time it was
actually detected. Missing sessions and sessions whose outcome is `UNKNOWN` are not
counted, no report is invented for them and the window is not extended.

**Every invocation checks the deadline** — the three assessments, a
reconstruction, and the five-minute liveness run — so it is detected on the
first one at or after 16:30, including the first after any downtime. The
check uses only the reviewed calendar, the stored observer records and the
current time. It reads no trading data and does not depend on the fifth
report existing, on Healthchecks.io being reachable, or on a notification
being delivered. The checkpoint keeps both `window_deadline_at_ist` and
`detected_at_ist`.

The fifth session can only be named when every date up to it is reviewed. If
one is not, the state reports the first unresolved date and the window stays
`UNKNOWN`; no date is invented.

The first after-close report for a date stands. A later report for the same
date is kept and listed but changes nothing unless an operator approval
explicitly supersedes the first. When a reason is reached the observer writes
`checkpoints/review-pending.json` once. Its `checkpoint_id` is derived from
the phase start, the reasons and the trigger session, so it is the same
whichever run records it. That record is never replaced. Later sessions are still recorded, marked
`outside_bounded_evaluation_period`.

### Owner attention

A report can correctly read:

```
outcome             QUALIFIED
bounded_phase.phase REVIEW PENDING
attention_required  true
attention_reasons   ["EARLY_REVIEW"]
```

Attention means the owner must look. It does not by itself say the session's
collection was invalid, and the session's qualification is never changed to
raise it.

Attention stays required until the operator acknowledges the exact
checkpoint. An ordinary successful assessment does not clear it.

**One stored notification per checkpoint.** Every invocation reconciles the
checkpoint with its notification intent
(`outbox/notifications/attention-<checkpoint_id>-intent.json`). If the
checkpoint exists, is unacknowledged and the intent is missing — because the
run that recorded the checkpoint was interrupted, or was a reconstruction —
the intent is written then. Its name is fixed by the checkpoint and it is
published with the same create-or-leave link as every record, so repeated or
concurrent reconciliation leaves exactly one. It is written whether or not
pings are switched on.

```sh
operator_acknowledge.py --report-root /var/lib/quantdesk-observer \
    --acknowledgments-root /etc/quantdesk-observer/operator/acknowledgments \
    --checkpoint-id <id> --text-file note.txt --acknowledged-by <operator>
```

An acknowledgment is bound to the checkpoint's id and SHA-256. It ends the
retries that belong to that checkpoint. It does not reopen the phase, approve
an operational exception, erase the attempt history, acknowledge any other
fault, authorize research or authorize any deployment, and it sends no
success: the after-close check returns to success only when a later
assessment supports it. It is a different record, in a different directory,
with a different scope from an approval, and the accounting never reads it.
The observer has no code path that writes one.

## Records

Stored under `report_root` (default `/var/lib/quantdesk-observer`), outside
QuantDesk's database and `research/`:

```
reports/<session-date>/<check>-<start-utc>-<id>.json
corrections/<session-date>/correction-<utc>-<id>.json
checkpoints/review-pending.json
outbox/events/<session-date>-<type>-<subject>.json
outbox/notifications/<id>-intent.json
outbox/notifications/<id>-round-<n>-reservation.json   written before the request
outbox/notifications/<id>-round-<n>-outcome.json       written after it returns; counts
                                                       only once checked and synced
outbox/notifications/<id>-attempt-<n>.json             versions 0.1 and 0.2; read only
outbox/heartbeats/<utc-date>/<utc>-<id>-reservation.json, -outcome.json
faults/<fault-id>-fault.json, <fault-id>-resolution.json
state.json      cache of the phase state, rebuilt from the records
notification-state.json   cache of what is still undelivered, likewise
lock
```

- **Publication.** A record is written to a hidden scratch file, synced, and
  hard-linked to its final name. The link fails if the name exists, so nothing
  is overwritten and a half-written record never appears under a final name.
- **Integrity.** Every record the observer writes carries the SHA-256 of its
  own content. Every read checks that hash, the schema version
  (`observer_report/3` is the only report schema accounted for) and the
  required fields and types, and that the file's name and folder match the
  report inside it.
- **Corruption.** A record that fails any of those checks — including one that
  still parses as JSON but holds only a schema label — raises an explicit
  failure. The run reports it, withholds the accounting, and requires
  attention; it does not skip the record.
- **Serialization.** Runs, corrections and checkpoint changes hold the same
  lock.
- **Report contents.** Scheduled time, deadline, actual start and completion
  (UTC and IST), lateness, observer version, QuantDesk revision expected and
  observed, session calendar status, result, every check with its evidence,
  the `UNKNOWN` fields and the failure reasons.
- **Corrections.** `observer.py correct` attaches a note to a report. It has
  no effect on qualification or the phase.

Healthchecks.io keeps its own short history. That is not the evidence archive;
these files are.

## Operator records

Three kinds, all under `operator_root` (default
`/etc/quantdesk-observer/operator`), each in its own directory. The observer
reads them and has no code path that writes any of them.

| Directory | Tool | What it does | What it does not do |
|---|---|---|---|
| `approvals/` | `operator_approval.py` | Lets one `OPERATIONAL EXCEPTION REQUIRED` session count, or lets a later report stand in place of an earlier one | Acknowledge a checkpoint |
| `acknowledgments/` | `operator_acknowledge.py` | Clears owner attention for one checkpoint | Approve an exception, reopen the phase |
| `maintenance/` | `operator_maintenance.py` | Explains planned inactivity in the reports | Pause a check, silence an alert, change a result |

Permissions (documented, not provisioned): the directories are owned by the
operator account, mode 0755, files 0444; the observer account can read them
and cannot write them; the unit sets `ProtectSystem=strict` and
`ReadOnlyPaths=/etc/quantdesk-observer`.

```sh
operator_approval.py --report-root /var/lib/quantdesk-observer \
    --approvals-root /etc/quantdesk-observer/operator/approvals \
    --session-date 2026-10-14 --report-id <id> \
    --scope count_session_under_operational_exception \
    --exception "08:45 readiness MISSING: VM started 09:03" \
    --astra-decision-reference "<where Astra's decision is recorded>" \
    --approval-text-file decision.txt --recorded-by <operator>
```

An approval holds the report id, the SHA-256 of that report file, the session
date, the specific exception, the Astra decision reference, the preserved
approval text, the recording operator, the recording time and the scope. The
observer accepts it only if the report exists for that date, the hash
matches, the scope is one it knows, every field is filled, and (for counting)
the report's outcome is `OPERATIONAL EXCEPTION REQUIRED`. Anything else is
listed under `rejected_approvals` and counts nothing.

A typed name is not authentication. What protects an operator record is who
may write to its directory. No signing infrastructure exists.

## Session calendar

Astra requires official NSE sources: the trading-holiday circular for the
segment, any later exchange notice, and the segment's market hours. Broker
calendars are corroboration only.

`session_calendar.json` lists every date from 14 Oct to 31 Dec 2026 with its
session type, hours, segment, official reference, superseding notice,
reviewer, review time and review status. **Every entry is `UNREVIEWED`.**

- **Where the dates came from:** QuantDesk's `market_calendar.py`, where 2026
  is marked provisional.
- **Candidate official source:** NSE circular `NSE/FAOP/71777` dated
  12 Dec 2025, "Trading holidays for the calendar year 2026" (F&O segment).
  It was located by search; its text could not be retrieved during this
  preparation, so no date has been checked against it. It is recorded as
  `candidate_reference`, and `official_reference` is left empty for the
  reviewer.
- **Not searched:** exchange notices issued after the annual circular.
- **Special session:** 8 Nov 2026 (a Sunday, Diwali Laxmi Pujan) is listed as
  `SPECIAL` for Muhurat trading. Its hours are not known here; NSE notifies
  them by a separate circular.

A date decides nothing until its own entry is `REVIEWED` with a reviewer, a
review time and the official reference. Until then:

- the date is `UNKNOWN`;
- no `NO SESSION` result is issued for it;
- a session on it cannot be `QUALIFIED` — the best outcome is `OPERATIONAL
  EXCEPTION REQUIRED`;
- the five-session window cannot be named past it.

Observations are never dropped on the calendar's word:

- Observations on a reviewed closed date fail the run, with their count and
  ids recorded and an event raised.
- Each after-close run lists earlier days that hold observations but have no
  after-close report, flagging weekends and reviewed closed dates.
- A failed read on a reviewed closed date is a failure, never `NO SESSION`.

A reviewed `SPECIAL` session is recorded with its observation count and
result `SPECIAL SESSION`. It is not run through the 75-observation template,
does not count as a normal session, and raises an event for owner review.
Schedules for special sessions are not prepared and need separate review.

The timers fire Monday to Friday and the observer then consults the calendar.
The hosted schedules cannot encode holidays; see below.

## Healthchecks.io

Hosted Healthchecks.io notices the pings the VM stops sending, which the VM
cannot report about itself. No second VM and no public endpoint are needed:
every request is outbound HTTPS from the observer.

Pings are **off** until `healthchecks.enabled` is true. Nothing here has sent
a real request, no check is provisioned and no real credential exists.

Four check identities, and no others:

| Check | Hosted schedule (timezone Asia/Kolkata) | Grace | What a success means |
|---|---|---|---|
| `liveness` | simple, period 5 minutes, 24 hours a day, 7 days a week (continuous operation); under Option A scheduled operation, OnCalendar `Mon..Fri 07:45/5` and `Mon..Fri 08..16:00/5` with 10 min grace (see `../power/monitoring/healthchecks.json`) | 5 min (Option A: 10 min) | the observer ran and reached out; nothing about QuantDesk |
| `premarket` | cron `35 8 * * 1-5` | 10 min | readiness passed by 08:45 |
| `midsession` | cron `30 12 * * 1-5` | 15 min | the 12:30 check passed by 12:45 |
| `afterclose` | cron `0 16 * * 1-5` | 30 min | **After-close assessment and observer evidence-accounting attention**: the assessment completed by 16:30, no review checkpoint is unacknowledged and no observer fault is open |

What is sent:

- **Success** only for today's assessment that passed and finished in time,
  and on a `NO SESSION` day.
- **Fail** (the provider's `/fail` endpoint) when the assessment failed, was
  late, timed out, hit an observer fault, or was a special session.
- **After-close fail for owner attention.** While a checkpoint is
  unacknowledged, every after-close ping is `/fail`, even when the session is
  `QUALIFIED`. The body carries `attention=YES reason=<reason>`. It means
  owner attention is required, not that collection was invalid. If the
  checkpoint is found by another run (for example on recovery from downtime),
  an after-close `/fail` is sent at once.
- **Nothing** for a run that did not happen, a run that started early, or a
  reconstruction. The hosted check alerts when its grace passes.

If the run that finds a checkpoint is not today's after-close assessment, the
checkpoint's own `/fail` is sent on the after-close identity. When today's
after-close assessment is sending a `/fail` anyway, that one request names
the checkpoint and is recorded against both intents; a separate request is
skipped only then.

**Limits on every invocation** (fixed in code, not configurable):

| Limit | Value |
|---|---|
| Requests per check identity | 1 |
| Requests in total, liveness included | 4 |
| Timeout per request | 5 seconds |
| All notification work | 30 seconds of wall clock from the first notification-specific work, one deadline for the invocation, enforced by an interval timer over the intents, the preparation, the reservation, the request, the outcome, the cache and the handling of their failures |
| Waiting or retrying inside an invocation | none |

**Retries across invocations.** There is no retry loop, timer or background
process. A later observer invocation — an assessment or the five-minute
liveness run — makes the next round if one is due.

| Round | Earliest |
|---|---|
| 2 to 6 | 5 minutes after the previous round |
| 7 onward | 1 hour after the previous round |
| Final round | none: an unacknowledged checkpoint's attention is never given up |

Only a `/fail` that is still the current statement for its check is retried:

- An assessment failure is dropped once a newer assessment of the same check
  exists, so an old failure never lands on a newer result.
- A checkpoint's attention ends with the acknowledgment of that checkpoint.
  So does an after-close failure that existed only because of it. A failure
  with its own cause continues.
- A delivered intent is not sent again.
- An undelivered **success or heartbeat is never sent late**, and no success
  is ever sent to make a check recover or to force another alert.

**Reservation before transmission.** Nothing is sent that is not already on
disk.

1. The intent is written to `outbox/notifications/` before any request.
2. Under the observer lock, a **reservation** is written for the request about
   to be made: its own id, the check, every intent the request speaks for,
   each intent's round number, the start time and each intent's next eligible
   time.
3. The request is made.
4. A separate **outcome** record is written: completion time, result, any
   sanitized error and the delivery state.

Neither record is ever replaced. If the reservation cannot be written, the
request is not made.

**An interrupted request.** If the process stops after the reservation —
before the request leaves, or after it left and before the outcome was
written — the reservation stands alone. The observer cannot tell those two
cases apart and does not try to:

- it counts as a round, and spacing runs from its reserved time;
- it is shown as "Notification undelivered; remote receipt unknown.", with
  completion `unknown`;
- it is not marked delivered and not assumed to have failed;
- nothing is sent again until the approved interval has passed.

After that interval the same `/fail` may go out again, so the far side can
receive it twice.

**An outcome counts only once it is durable.** A readable, linked outcome
file is not enough. Every reader — the writing process, a new reader in it,
and a restarted process — relies on an outcome (notification or heartbeat)
only after checking it against its reservation (schema and seal; reservation
id, identity, kind and reservation time; `delivered` and `delivery_state`
agreeing) and syncing it and every directory above it, afresh on every read.
Until then the reservation stands as above: a round of unknown completion and
remote receipt, with the retry spacing running from it. If writing the outcome
fails or is interrupted at any point — before its final link, while its
directory is synced, or after — the process that wrote it never relies on it,
and says the request's receipt is unknown; the surviving bytes are left as
they are. A later process that can check and sync them may rely on them then;
one that cannot sync still does not. An outcome that names another
reservation, identity, kind or time, or contradicts itself, is damaged: the
history is shown as unresolved and nothing is sent again automatically.

**A consolidated request.** One request on the after-close identity can speak
for several stored events at once (an assessment failure, a review
checkpoint, an observer fault). It has one reservation and one outcome, and
each included intent's history points to that same reservation. It counts as
one request. An intent whose own retry is not yet due is not included.

The round count and spacing are read from these files, so a new process
continues where the last one stopped. Every invocation rewrites
`notification-state.json` with each outstanding intent's rounds, last
attempt, last error and next eligible time, and liveness prints them — also
when no retry is yet due. `observer.py status` shows the same.

**Which assessment is current.** The stored assessment reports decide, not
the notification history alone. A report made while pings were switched off
is still the check's newest assessment, so an older undelivered `/fail` for
that check is not sent once pings are back. These do not count as a newer
assessment: a run that started early, a reconstruction (`--session-date`), a
run that found the lock held, a report that fails validation, and a report of
another check. No delayed success is sent to reset the hosted check; it
recovers at the next assessment. A report written before version 0.3.0 does
not record whether it was a reconstruction, so it supersedes nothing by
itself.

**Delivered is not acknowledged.** Three separate facts:

| Word | Meaning |
|---|---|
| Delivered | the monitoring endpoint answered 2xx |
| Acknowledged | the operator acknowledged the exact checkpoint, with a validated record |
| REVIEW PENDING | the research window reached its review checkpoint |

While the checkpoint is unacknowledged every after-close request says
`attention=YES`, whether or not an earlier one was delivered.
`attention=ACKNOWLEDGED` appears only on an older failure sent after a
validated acknowledgment of that same checkpoint.

**Records from earlier versions.** Intent and attempt records of schema `/1`
(version 0.1.0) and `/2` (0.2.0) are read as they are and never rewritten,
renamed, migrated or deleted. New intents are still written as `/2`; new
attempts are reservation and outcome records. Version 0.4.4 also reads
everything 0.3.0 to 0.4.3 wrote, with two deliberate differences: a report
from before 0.4.3 has no confirmation, so it is kept for diagnosis and counts
for nothing; and a publication-fault resolution written before 0.4.4 lacks
`report_path`, so it is not recognised and its fault stays open (nothing
missing is inferred). Reservation, outcome and fault records keep their
schemas, and fault records gain two optional fields (`kind`,
`detected_by_invocation`); a 0.3.0 fault without them is resolved by a later
invocation that completes the operation named at the start of its signature. Old and new history are joined
in memory only.

- A `/1` attempt made up to five requests. Each is counted as a round, and
  spacing runs from the latest time anywhere in the record.
- A `/1` checkpoint intent and the current one are the same alert when they
  name the same checkpoint. No second intent is created, and if both exist
  only the older is acted on.
- Where an old record is silent on something the retry rule needs, the intent
  is shown as "history unresolved" and is not sent again automatically.
- A record that is corrupt, of an unsupported schema, of the wrong shape,
  contradictory or orphaned is refused and raises an observer fault. It is
  never dropped silently and never treated as permission to send.

**What a delivery record means.**

- A failed request is recorded as "Notification undelivered; remote receipt
  unknown." A timeout can follow a request the far side did receive.
- A delivered request means the endpoint answered 2xx. It is not proof that a
  Telegram message or an email reached anyone.
- One request may speak for several stored events on the same check. Not
  every historical event is individually delivered.
- The observer does not promise exactly-once delivery; a duplicate is
  possible after an ambiguous failure.

TLS certificates and host names are verified. Redirects and proxies are
refused. The body is one line of status with no credentials, trading
records, quotes, positions or logs.

Credentials: one ping UUID per check, in a separate file
(`/etc/quantdesk-observer/healthchecks.json`, mode 0600, owned by the observer
account; see `healthchecks.json.example`). The file is refused if other
accounts can read it, if a UUID is malformed, or if two checks share one. The
UUID appears only in the request URL and is redacted from every record,
message and error.

### Liveness, around the clock

The liveness timer fires every five minutes, every day. A missing heartbeat
means **"heartbeat missing — cause unknown"**. It does not show that the VM is
powered off, and nothing assumes the VM is stopped overnight.

Planned inactivity needs an explicit maintenance record (start, end, reason,
affected checks, authorization reference) written with
`operator_maintenance.py`, and a separately authorized manual pause and
resume in Healthchecks.io. The observer copies active maintenance records
into its reports as information. It does not pause a check, and it never
powers on, stops or restarts the VM.

A missed liveness ping is recorded locally and is not sent late.

On a liveness invocation the heartbeat is sent first. Then, under the lock,
the observer does its local housekeeping (the window deadline, the
checkpoint, its notification intent) and one delivery round. The
housekeeping runs whether or not the heartbeat got through and whether or not
pings are switched on. The heartbeat keeps its one meaning: recent observer
execution and outbound connectivity.

### Observer housekeeping failures

If the housekeeping fails — for example because a stored report no longer
validates — the same invocation:

1. records an independent fault in `faults/`, with reason code
   `observer_housekeeping_failure`;
2. stores a `/fail` intent on the **after-close** identity;
3. sends it at once, if the after-close identity is unused and the request
   and time budgets allow; otherwise it stays due for a later invocation;
4. exits non-zero and prints the fault.

The ping carries only `reason=observer_housekeeping_failure` and the fault's
id. No path, diagnostic text or record content leaves the machine.

- **Not a data verdict.** A fault changes no report, outcome or count.
- **No dependence on the accounting.** Recording the fault and its intent
  reads no report, so corrupt evidence cannot prevent the alert. The corrupt
  record is left exactly as it is.
- **One fault per kind of failure.** A failure that repeats every five
  minutes is one open fault.
- **With a checkpoint.** Both are named in one after-close request; both
  local records remain.
- **Kinds of fault.** `housekeeping` (including an error in the phase
  calculation), `notification-round` (a delivery round failed),
  `notification-state` (`notification-state.json` could not be written) and
  `report-publication` (one fault per affected report; see above). For
  the last, the fault and its intent are recorded and one more delivery round
  is made within what is left of the invocation's allowance; nothing retries
  the cache or raises a fault about a fault.
- **Resolution.** A fault is resolved only by a *later* invocation that
  completes the same operation that failed, with its reconciliation: the
  housekeeping for a housekeeping fault, a delivery round for a round fault,
  and a successful write of `notification-state.json` after a successful
  housekeeping for a cache fault. Success at anything else does not resolve
  it. It writes a resolution record and an `observer_fault_resolved` event;
  the original fault stays. A delivered heartbeat does not resolve it, and
  neither does a checkpoint acknowledgment.
- **No synthetic recovery.** Resolution stops the retries and sends nothing.
  The after-close check returns to success only at the next successful
  after-close or valid `NO SESSION` assessment, with no fault open and no
  checkpoint unacknowledged.

**The notification deadline.** The 30-second budget is one monotonic
deadline for the whole invocation, held by the invocation's one notification
context, which every notification path uses. It is fixed immediately before
the invocation's first notification-specific work, whichever that is: an
alert intent for a newly recorded checkpoint or for an observer fault (which
may come before the report is even published), the notification side of the
housekeeping, the assessment's own intent, the delivery round, or the
notification-state cache; in a liveness run, the heartbeat. It is never
moved, restarted, paused or extended: not between identities or requests,
during the lock or accounting that follows it, after a retry decision, after
an exception or during fault handling.

Each invocation's work is in two parts:

- **Accounting** — the assessment, publishing and confirming its report,
  qualification and the bounded phase, the checkpoint and its events, the
  observer's own fault records for accounting failures, and the
  reconciliation of evidence. Never cut short by the notification deadline:
  it has its own limits, and it is done whatever the notification work did.
- **Notification work** — storing alert intents (an unacknowledged
  checkpoint's, an observer fault's, an assessment's), reading the
  notification history for delivery, attention routing and retry
  eligibility, re-verifying every report, confirmation, fault and resolution
  the round depends on, building, reserving and making the requests, writing
  their outcomes, the notification-state cache, the reconciliation and
  resolution of notification faults, and the recording of failures of any of
  this (its event, fault record and alert). All of it runs under an interval
  timer set to what is left of the deadline; work inside work (a request
  inside a round) shares that one timer. The time left is read again before
  each request: below one second nothing is sent, and the request's timeout
  is at most the lesser of five seconds and what is left.

When the deadline passes, notification work stops, all of it, at once: the
request it cut short keeps its reservation and its unknown completion, and
every enclosing piece of notification work stops with it — no later identity
is prepared, no further intent, failure event, fault, reservation, request,
retry, round, cache rebuild or fault resolution, and no fresh allowance for
any of them. Nothing interrupted is taken as done: evidence whose check was
cut off is simply not relied on, a round cut off resolves no fault, a
reservation written before the cut stands (as a round of unknown completion
and remote receipt, from which the retry spacing runs), and an outcome not
made durable is not claimed. What was not done is not lost: a later
invocation stores, from the records alone, the alert intent of every open
fault that has none, the intent of an unacknowledged checkpoint, and that of
the newest assessment of a check that did not pass and was to be notified
(`notification_planned` on its report); nothing else is made up, and a pass
is never sent late. The run prints one sanitized line (`NOTIFICATION
DEADLINE`, with where it happened and any overrun) and the requests it
withheld; that is not an observer fault, and nothing is queued or sent about
it. A storage call that the operating system will not interrupt (a stalled
disk) ends only when it returns, so the budget can be overrun then; the work
stops at the first point after it, and the overrun is reported. This is a
bound enforced where the system allows it, not a hard real-time guarantee.

**When notification preparation fails.** A failure is classified by the
operation that failed, not by the kind of error or where it was raised. If
the notification side of the housekeeping fails — a damaged notification
record found while reading the history, an I/O error while storing an
intent, or any other error there — that is a `notification-preparation`
fault, not a housekeeping one: its event, fault record and alert are written
inside what is left of the same deadline (no fresh allowance), and a failure
while writing them is only noted, never handled again. The accounting goes on
regardless: the state is persisted, publication faults are reconciled, the
checkpoint is recorded, and the assessment's result stays what the evidence
made it. The invocation still exits non-zero, and says which operation
failed and whether its fault was stored. A preparation fault is resolved
only by a later invocation in which every preparation stage succeeded — none
failed and none expired, including the second pass the housekeeping makes
after publication is reconciled — and only after that last pass, within what
is left of the deadline. Once preparation has failed or expired in an
invocation, nothing later in it can make it eligible; a report-publication
fault resolving in the same invocation says nothing about preparation.

If the deadline stops the recording, the run says so locally. Interruption
does not roll back completed durable writes. Intermediate or linked records
may remain, but their presence alone does not establish durable completion.
The observer reports verified outcomes and uncertainty; an interrupted fault
or alert publication is not claimed as durably stored without the required
verification. A damaged record is found again, and recorded, by a later
invocation. A failure of the accounting itself (persisting the
state, recording the checkpoint, reading a damaged report or fault) is still
a housekeeping fault, with the accounting after it withheld, as before.

**One timer at a time.** The deadline is enforced with the process's
interval timer, on the main thread only. It is never put over another
interval timer that is already running: an assessment's limit and the
notification deadline run one after the other. If one is already running
(or the work is not on the main thread), nothing is installed or changed —
the running timer keeps its handler, its remaining time and its deadline —
and the invocation does no notification work at all: it prints
`NOTIFICATION DEADLINE NOT ENFORCEABLE` and exits non-zero, with its
accounting complete. An assessment's limit is refused in the same way, which
makes that assessment fail as an internal error. On leaving, the deadline's
timer is cleared and the previous handler restored.

**When storage itself is unavailable.** If the observer cannot write a fault
record or an attempt reservation, reliable remote notification cannot be
guaranteed. In that case it exits non-zero, prints the storage error, claims
no delivery and sends nothing that is not reserved — including the liveness
heartbeat, whose absence is then what the hosted check reports. QuantDesk is
not changed to compensate.

### Alerts to Telegram and email (not connected)

Both are configured in Healthchecks.io, not on the VM, and need no credential
here. Telegram is the primary channel and email the secondary.

- **Telegram direct message:** in the project's Integrations, add Telegram,
  open the bot link it shows, and send the bot `/start` from the account that
  should receive alerts. Assign the integration to all four checks.
- **Email:** add an Email integration with the destination address, confirm
  it from the verification message, and assign it to all four checks.
- **Hourly email reminders:** in the Healthchecks.io account's email-report
  settings, turn on the reminder that repeats while any check is down and set
  it to hourly. Check the exact option name in the live interface when this
  is set up.

Healthchecks.io alerts on status changes. Repeated `/fail` pings while a check
is already down do not guarantee another Telegram message; the hourly email
reminder covers a check that stays down. The observer never sends a success
to force a fresh alert.

Send a test notification from each integration before relying on it. No real
delivery has been tested.

### Scheduling dependencies still open

- Hosted cron schedules cannot encode exchange holidays. The observer sends a
  success on `NO SESSION` days, but only for reviewed dates; an unreviewed
  holiday produces a `/fail`.
- The afterclose grace must be 30 minutes and the midsession grace 15, to
  match the completion deadlines.
- Special-session schedules are not prepared.

## Local events

Files in `outbox/events/`, written only for exceptions:

| `type` | When |
|---|---|
| `operational_failure` | a run failed, timed out, hit an observer fault, found the lock held, or could not deliver a ping |
| `observer_fault_resolved` | a later invocation completed the housekeeping that had failed |
| `bounded_window_ended` | the five-session window ended with unresolved evidence |
| `missing_check` | a later run finds no timely pre-market or mid-session run, or a day with observations and no after-close report |
| `early_review_trigger` | early-review checkpoint recorded |
| `five_session_checkpoint` | five qualifying sessions recorded |
| `operational_exception_required` | after-close outcome |
| `evidence_inconsistency` | outcome `INCOMPLETE` or `UNKNOWN`, activity on a closed or weekend date, a special session, or a corrupt record |

Fields: `schema`, `type`, `subject`, `session_date`, `check`, `report_id`,
`created_at_utc`, `observer_version`, `summary`, `integrity`. Checkpoint events
carry the checkpoint id in `subject`.

## Minimum privileges

Nothing below is provisioned. Each item needs its own review.

**Database** (a role for the observer only):

- `CONNECT` on the QuantDesk database;
- `USAGE` on schema `public`;
- `SELECT` on:

| Table | Columns |
|---|---|
| `signals` | `id, created_at, action, confidence, checks, code_id, strategy_version, parameter_hash, bar_open_time, provenance, context` |
| `paper_decisions` | `id, session_date, action, outcome, code, detail` |
| `paper_positions` | all columns (rows opened in the session are quoted in the report) |
| `trades` | `id` |
| `option_candles` | `session_date, exchange_time, capture_time` |
| `vix_daily` | all columns |

No `INSERT`, `UPDATE`, `DELETE`, DDL, sequence or function privileges. The
connection comes from libpq's environment (`PGSERVICE`, `PGSERVICEFILE`,
`PGPASSFILE`) in `/etc/quantdesk-observer/observer.env`; no connection string
is in the code or in `observer.json`.

**Filesystem:**

| Path | Observer account |
|---|---|
| `/opt/quantdesk-observer` | read |
| QuantDesk checkout | read (and git must accept the checkout as safe for it) |
| `/var/log/unattended-upgrades/unattended-upgrades.log` | read (group `adm`) |
| QuantDesk backend log (`logs/native/backend.log`, `.1` … `.14`, plain or `.gz`) | read |
| `/etc/quantdesk-observer/observer.json`, `session_calendar.json` | read |
| `/etc/quantdesk-observer/healthchecks.json` | read, owner only |
| `/etc/quantdesk-observer/operator/{approvals,acknowledgments,maintenance}/` | read only |
| `/var/lib/quantdesk-observer` | read and write |

The service must not be able to modify QuantDesk code, trading data, operator
records or systemd configuration, or to restart trading services. Running
it as `azureuser` would break that, because `azureuser` owns the checkout.

## Before installation

None of this is done.

1. Astra confirms the items listed under "Open items" below.
2. An independent reviewer checks each calendar date against the NSE circular
   and marks it `REVIEWED`.
3. The service account, the database role and the operator directories are
   provisioned under their own authorization.
4. `observer.json.example` is copied to `/etc/quantdesk-observer/observer.json`
   and checked: `expected_listeners`, the baseline counts, and that
   `signals.parameter_hash` holds the full hash.
5. The Healthchecks.io checks are created and their UUIDs placed in the
   credentials file.
6. The files are copied to `/opt/quantdesk-observer`, a first run is made by
   hand outside market hours and its report read, and only then are the
   timers enabled.

A run that ends in `FAIL` exits non-zero, so its unit shows as failed in
systemd. That is intended and affects no QuantDesk unit.

## Open items

- The real probe (`systemctl`, `ss`, the API, `psql`) has never run against a
  live system. Only its git path, its scheduler-log parser (on a synthetic
  log), and its loopback, redirect and size limits have been exercised.
- No calendar date is reviewed, so today no session can be `QUALIFIED` and
  the five-session window is `UNKNOWN`.
- Sessions between 14 Oct and installation have no contemporaneous observer
  reports. Reconstructing them gives at best `OPERATIONAL EXCEPTION REQUIRED`.
- The schedule depends on the scheduler and start-up lines described under
  "Observation schedule". They have not been read on the Azure VM. If any is
  absent or logged below INFO, the schedule is `UNKNOWN` and no session can
  qualify. Rotation is read through for up to 14 rotations (v0.4.9). Once an
  assessment has seen the running process's start, its original bytes are
  kept and reused for that process only (v0.4.12). The rotated-file reader
  and the capture have been tested only on synthetic files, not on the Azure
  VM. The state directory's ownership and permissions must be checked there
  before deployment.
- No schedule is established until `backend_log_basis` records a verified
  logging zone (Asia/Kolkata) and a single writer for the backend log
  (v0.4.10). Both are unverified on the Azure VM.
- A late-started run is bounded at 180 seconds of work. That bounds execution
  only; it never makes a late run timely. A run stopped by its limit keeps
  the checks it had finished and records `timed_out`.
- Every liveness heartbeat leaves a reservation and an outcome file (about
  580 small files a day). By Astra's decision they are all kept through the
  bounded phase and its closing review: nothing deletes, prunes, archives or
  compacts them. Long-term archival needs a separate decision.
- The feed often goes quiet shortly after the close. If that happens before
  the 16:00 snapshot, operational continuity is `UNKNOWN` and the session
  needs an exception.
- `GATE_ORDER`, `GATE_CODES` and the evidence field lists restate the
  producer's contract at `2d1d25a71394`. A change there would make decisions
  read as inconsistent until the observer is updated.
- Real notification delivery has not been tested.

## Shutdown evidence (v0.4.14)

Option A runs the VM by day only: Azure Automation starts it at 07:30 IST on
weekdays, and a separate power controller (`../power/`, its own README) may
deallocate it from 17:30 IST. The controller asks the observer one question
first, and only through this read-only interface:

```
observer.py shutdown-evidence --config FILE --txn <32 hex> [--lock-fd N] [--api-stopped]
```

It prints one sealed `observer_shutdown_evidence/1` record and exits. It
writes no file, sends no notification and makes no ping (the notification
machinery is never constructed), and it changes no assessment, qualification,
notification, checkpoint or history semantics. It reads only authoritative
records through the readers every assessment uses: confirmed after-close
reports (`Store.reports`), the bounded phase (`phase_state`), notification
records (`notification_states`, never `notification-state.json`), faults
(`Store.faults`) and the reviewed calendar. Caches are never read.

It reads under the observer's own lock: a descriptor the controller already
holds and passes in (`--lock-fd`, verified to be the lock file itself), so no
observer run, scheduled or manual, can start between the controller's checks
and its shutdown; or, run by hand, the lock file opened without creating it.

The record is bound to the transaction id, the boot id, the API process
systemd reports (MainPID and its monotonic start; with `--api-stopped`, the
stopped unit's last start), the observer version, and the SHA-256 of the
configuration and calendar bytes.

| Verdict | When |
|---|---|
| `UNKNOWN` | the lock, the API identity or any record cannot be read or checked; a notification history is unresolved; a fault resolution does not check out |
| `BLOCKED` | no confirmed after-close report today when today is a reviewed normal session or not reviewed at all; a review checkpoint is due and not recorded; a current `/fail` is undelivered; an observer fault is open; a reviewed special session is today or tomorrow |
| `CLEAR` | none of the above |

Only `CLEAR` lets the controller continue, and the controller checks every
binding itself. A delivered `/fail` does not block: its hosted check stays
down overnight until a later legitimate success. An undelivered one does: the
VM stays up and the observer keeps retrying.

**15:40 and 15:30.** The official F&O normal-market close is 15:40 IST from
3 Aug 2026 (NSE/FAOP/74467). QuantDesk collects 09:15–15:30 and the observer's
`SESSION_CLOSE` is 15:30; both are the collection model and are unchanged.
The evidence interface uses neither, and the controller acts only from 17:30.

## Tests

`tests/test_observer.py` runs the observer against a fake probe, a recording
ping transport and a temporary directory. It touches no VM, no QuantDesk API,
no database and no Healthchecks.io endpoint.

```sh
.venv/bin/python -m pytest tests/test_observer.py -q
```
