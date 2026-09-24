# Diagnostic reliability and recovery

## Owner boundaries and ordinary use

TimeAudit owns collection, time-state semantics, historical aggregates, shared
health and its database backups. PCConfig owns native memory evidence, current
machine counters, bounded Windows events and the combined diagnosis. The
`timeaudit-diagnostics` skill routes requests and explains evidence; it does not
become another collector. Network publication, ports and firewall policies are
outside this change and remain unchanged.

```powershell
# A recent responsiveness incident. Read-only; no automatic recovery.
& E:\PCConfig\tools\Get-ComputerStutterDiagnostic.ps1 -Minutes 15 -Json
# Use both explicit timezone-bearing bounds for an older incident; -SkipLive
# makes the omission of current measurements explicit.
& E:\PCConfig\tools\Get-MemoryFreezeEvidence.ps1 -CoverageOnly -Json
& E:\Projects\Tools\TimeAudit\.venv\Scripts\python.exe -B E:\Projects\Tools\TimeAudit\timeaudit_health.py
# Optional two-second observation of the exact main collector and children.
& E:\Projects\Tools\TimeAudit\.venv\Scripts\python.exe -B E:\Projects\Tools\TimeAudit\timeaudit_health.py --include-overhead
```

An unavailable or partial source is missing evidence, not a normal result. A
threshold, application crash, storage reset or large memory pool is a candidate,
not a proved cause. Native memory committed bytes and commit limit are paired at
the same sample time. Retained rolling span, discontinuous prior-session tails
and actual queried coverage must remain separate. An open BLG segment is not
read. The native reader aggregates inside Windows PowerShell before returning
JSON, so compatibility-remoting object conversion cannot corrupt interpretation.

The GUI status entry, AI health entry and watchdog acceptance use the same
health provider. `--core-only` deliberately excludes backup/blackbox/previous
watchdog outcome; it cannot recursively validate itself. Exit 2 denotes a valid
unavailable/degraded diagnostic result. A recent heartbeat does not establish
successful activity persistence. The last watchdog outcome is separate evidence.

The existing watchdog also restores the Docker dependencies before checking
collectors. If Docker Desktop has exited, it invokes the existing registered
`TimeAudit_AutoStart` task unless that task is already running, and checks
readiness on the next minute's cycle. This preserves the registered interactive
startup context instead of duplicating it in an ad hoc process. An already running but
unavailable engine is reported as unavailable; the watchdog never restarts the
shared engine. Existing stopped PostgreSQL and Grafana containers are started
without recreating containers, volumes or data. Grafana's local `/api/health`
must report its database `ok`; a running Grafana that still fails after a grace
period can be restarted individually. Docker commands have bounded waits, and
an unconfirmed mutation is inspected on the next cycle rather than replayed.
Shared health includes Grafana, so collectors alone cannot make the service
healthy. Public HTTPS reachability is a separate tunnel/publication check.
Recovery does not reconstruct telemetry that was never collected.

Autostart keeps an existing exact `TimeAudit.ahk` process; unhealthy activity
capture remains the watchdog's responsibility. Bootstrap uses Compose
`up -d --no-recreate`: it starts or supplies missing services, but does not
apply configuration changes by replacing existing containers. Maintenance must
explicitly deploy a configuration change. Grafana now uses host port 43000;
53000 was inside a Windows excluded dynamic range and caused a real bind
failure. All local health, backup/restore defaults and the registered tunnel
origin must agree when this port changes.

## Measurement semantics

A scheduled hardware slot is consumed only after its monotonic deadline. Early
wakeups yield again; slow work skips missed slots rather than chasing a backlog.
The Windows mutex must be successfully created and acquired as a singleton;
creation failure does not authorize a second collector.

CPU package temperature/power and GPU core-hotspot fields contain real, fresh
sensor readings or NULL. ACPI zones, memory-junction temperatures, CPU-load
formulae and core-temperature offsets are not substitutes. Missing disk/paging
or frequency counters are NULL; a real zero remains zero. Cached sources expire.

New hardware rows include additive `measurement_quality` (contract 2),
`collector_instance_id` and `collector_sample_seq`. Old rows are not rewritten.
Legacy provenance and missing readings remain visible in aggregate output.
RTSS `current_fps` is the latest frame's reciprocal; average FPS is a window
statistic. Legacy RTSS rows use a separate source-aware compatibility path, not
an assumption that window FPS and the last frame are exact reciprocals. FPS
lifecycle distinguishes active, gated idle, starting, waiting and source failure.

Historical observer metrics cover the main collector only. Their CPU unit is
100 percent per logical processor. Optional live overhead includes that exact
collector's children, but excludes standalone sensors, the Docker ingester and
native recorder. Summed process RSS includes shared pages and is not unique RAM.

## Time-state persistence

Time-state intervals are UTC. An idle transition cannot rewind before an
already assigned interval. Timer delay or clock discontinuity becomes
`System_CollectionGap`; only a Windows suspend notification establishes sleep.
Lock, display-off and idle remain distinct states. State mutations are serialized.

The activity writer publishes immutable UTF-8 CSV segments by writing a temporary
file, flushing it, then atomically renaming to `buffer.csv.ahk.*.processing`.
The existing ingester accepts this pattern and removes a file only after its
transaction commits. Content-based deduplication protects replay.

A failed write retains its bounded pending queue and does not advance the
committed watermark. At the queue limit, detailed events become an explicit
unknown interval, not falsely attributed application use. `ahk_health.json`
reports pending events, write/overflow/capture/clock failures and publication
counts. There is no zero-loss promise: abrupt power loss can lose the current
unsealed interval, and a physically unwritable disk cannot durably retain RAM.
Normal active intervals seal at most about 30 seconds apart or on a transition.

## Anomaly consumer reliability

`timeaudit:pcconfig-hardware-anomaly.v2` uses qualifying consecutive observations
at most 2.5 seconds apart and measured elapsed time, not scattered sample counts.
A fixed 31-second lookbehind preserves a run crossing the cursor boundary;
reported counts and first/last timestamps stay in the requested window.
The aggregate query is read-only with server statement/lock and client budgets.
Malformed fields, non-finite numbers and unexpected payloads fail closed.

PCConfig durably records `pending_projection_refresh` before a downstream
configuration recheck. A failed or interrupted effect remains pending after
advancing the consumed-data cursor. It retries even when no new source window
exists. Legacy cursor profiles migrate; no anomaly directly changes hardware.

## Database backups and independent verification

`backup_db.ps1` retains its existing scheduled-task interface and calls the
project's qualified Python runtime. A binary export goes to a unique `.partial`
file, is flushed, checked as a PostgreSQL custom archive, listed and SHA-256
hashed before becoming a completed `.dump` with an atomic `.dump.json` manifest.
Failed exports never replace a completed archive. Retention keeps at least three
completed archives and removes only eligible old, verified pairs. Unverified
historical originals are preserved rather than silently classified as disposable.

```powershell
# Read verification, or explicitly record integrity metadata.
& .\.venv\Scripts\python.exe -B .\timeaudit_backup.py verify
& .\.venv\Scripts\python.exe -B .\timeaudit_backup.py verify --record
# Explicit recovery drill, NOT part of ordinary health diagnosis.
& .\.venv\Scripts\python.exe -B .\timeaudit_backup.py restore-check
& .\.venv\Scripts\python.exe -B .\timeaudit_backup.py restore-status
& .\.venv\Scripts\python.exe -B .\timeaudit_backup.py finish-restore
```

Restore checks use the already-installed immutable PostgreSQL image, no network,
no published ports, a read-only archive mount and an independent disposable
database. They never import into `audit-postgres`. A container-local completion
record survives loss of the initiating client. The journal serializes start and
finish; ownership labels are checked before cleanup. The worker has a bounded
execution time. `running`/`starting` is not PASS. `finish-restore` verifies the
completion code and readable restored tables, records the outcome, and removes
only its identified temporary container and anonymous volume. Finish a running
operation before starting another. Inspect unavailable evidence rather than
blindly retrying create or destroying an unidentified container.

A lightweight health read checks manifest presence, shape, size and freshness.
It does not rehash gigabytes or run a restore. Archive integrity, prior full
restore proof and current runtime health are three different claims.

## Source, installation and activation

The database migration adds only three nullable provenance columns. Existing
collectors and backups remain readable. A rollback to the prior source does not
require dropping columns or rewriting history. Keep source recovery in Git.

After source validation, replace only the exact TimeAudit main collector and
AHK instance through the existing watchdog ownership/serialized launch pattern.
Do not kill all Python/AHK processes, restart Docker or reboot the PC. A normal
AHK replacement should allow its exit handler to flush. Keep startup grace and
verify fresh contract-2 database rows, unique sampling cadence, activity
persistence, ingester backlog and shared-health results. A code commit alone is
not activation; a synthetic test is not a fresh-agent or power-loss test.

Tests: project pytest regressions; native `test_timeaudit_state.ahk`; PCConfig
`Test-MemoryFreezeEvidence.ps1`, `Test-StutterEvidenceContract.ps1` and
`timeaudit_anomaly_increment.test.ps1`; .agents
`Test-TimeAuditDiagnosticsSkill.ps1`. Use an E-drive task temporary directory
and leave production dependencies unchanged when installing test-only packages.
