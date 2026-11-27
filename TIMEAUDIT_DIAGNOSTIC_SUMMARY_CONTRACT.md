# TimeAudit diagnostic summary contract

TimeAudit owns telemetry collection and the read-only provider
`timeaudit_diagnostic_summary.py`. The provider supplies bounded historical
evidence for personal computer diagnosis; it does not diagnose causality and it
does not control the collector.

## Interface

```powershell
& E:\Projects\Tools\TimeAudit\.venv\Scripts\python.exe -B E:\Projects\Tools\TimeAudit\timeaudit_diagnostic_summary.py `
  --after-utc <exclusive-UTC-bound> `
  --until-utc <inclusive-UTC-bound>

# Quick relative window
& E:\Projects\Tools\TimeAudit\.venv\Scripts\python.exe -B E:\Projects\Tools\TimeAudit\timeaudit_diagnostic_summary.py --hours 3
```

With no bounds it reads the latest 24 hours. `--hours` is the fast path for a
relative window and cannot be combined with exact bounds. A request must be at most 168
hours. The command performs one aggregate-only query through `docker exec` and
the existing `audit-postgres` container, so the caller never receives or
provides a database password.

Successful output is `timeaudit.diagnostic-summary.v1` and includes:

- requested window, sample coverage, freshness, boundary gaps and the largest
  internal-or-boundary gap;
- aggregate CPU/GPU, memory, disk and network ranges;
- FPS/1% Low/frame-time statistics only for positive, physically plausible
  new-source samples whose FPS and frame time agree within a 35%/3ms tolerance. Legacy RTSS rows lacking measurement provenance use their explicitly separate window-FPS compatibility, including
  internally consistent severe low-FPS stalls; rejected positive samples are
  counted separately instead of being treated as gameplay;
- unioned active, idle, display-off, lock, sleep, foreground-hung, capture-unknown
  and collection-gap durations, plus uncovered and cross-state-overlap seconds;
- bounded threshold-occurrence signals and explicit interpretation limits;
- negative privacy flags proving that raw/private payload classes are absent.

Signals are correlations and threshold occurrences, not proof of a root cause
or of consecutive/sustained pressure. An uncovered interval can be normal
sleep/power-off or a collection gap. User-space scheduler jitter is not kernel
DPC latency.

## Privacy and effects

The provider never returns raw samples, process names or paths, window titles,
command lines, remote addresses, credentials, or machine identifiers. It does
not call Grafana or PCConfig, write cursors/receipts, publish configuration,
start or restart Docker/TimeAudit, or mutate the database.

With `--live-output <JSON>` the same producer instead writes the project's small
website snapshot through `live_project_snapshot.py`; it selects counts, date
bounds and clipped interval unions, never clipboard payloads. The live snapshot
requires an exact 24-hour window and exports the peak working set in decimal MB.
The diagnostic JSON reports the peak in MiB. `--live-static`
refreshes source size on the existing daily backup run. Its tracked snapshot
contains only source size; unchanged source retains its bytes and timestamps.
The same run writes slow exact counts (`hardware`, `lifecycle`, `registry`) and
the earliest activity/window dates to the configured runtime `retained_output`.
Both audience contracts read these facts and their measurement times from
`timeaudit.retained`, with the existing two-day freshness threshold. These exact
queries stay outside the two-minute refresh's 35-second process bound.
The existing watchdog refreshes live facts every two minutes with a 35-second
process bound. The snapshot reads clipboard and dashboard files independently
of its bounded database reads. If an individual source fails, its last
successful value and timestamp are retained when available. Dashboard and
viewer definition dates come from the corresponding source files' Git history.
Private paths and owner output locations come from ignored `live.local.json`
(a link to the private companion), or explicit `--live-config <JSON>`.
No new task or resident collector is installed. The regular diagnostic interface
above remains read-only and does not write a snapshot without this option.
Unchanged static values and source size retain the existing snapshot and times.

For the regular diagnostic read, missing Docker/PostgreSQL, a timeout or
invalid/oversized query output returns a bounded `status=unavailable` JSON
document and exit code 2. Invalid arguments return the same error form.

## Verification

```powershell
& .\.venv\Scripts\python.exe -B -m unittest -v test_timeaudit_diagnostic_summary.py
& .\.venv\Scripts\python.exe -B timeaudit_diagnostic_summary.py
# After committing source changes, with the private live.local.json available:
1..2 | ForEach-Object {
    & .\.venv\Scripts\python.exe -B timeaudit_diagnostic_summary.py --live-static --live-output docs\live.snapshot.json
    if ($LASTEXITCODE) { throw 'Static export failed' }
    if (git status --porcelain) { throw 'Static export dirtied the repository' }
}
```

## Additive quality and combined diagnosis

The v1 envelope includes data_quality: distinct sampling seconds, rapid adjacent samples, collector instance count, missing readings, legacy provenance, and observer-cost aggregates. Historical observer cost is the main process only, not total stack cost. FPS capture-state counts distinguish frames, idle, waiting and source failure. System_CollectionGap is an unknown observation gap, not confirmed sleep.

The collector adds measurement_quality, collector_instance_id and collector_sample_seq without rewriting old rows. Missing sensor readings remain null; a fresh LHM NVIDIA temperature list without a core Hot Spot sensor reports `gpu_hotspot_source=unsupported` rather than substituting Memory Junction. PostgreSQL is read-only with a 12-second statement timeout and 1-second lock timeout; the wrapper bounds the process to 14 seconds/output and rejects unexpected aggregate fields or numeric types. The single hardware aggregation reads exact samples for the requested window; `activity_system_hour` summarizes process activity, not these hardware and FPS fields.

PCConfig Get-ComputerStutterDiagnostic.ps1 combines history with native BLG, bounded Windows events and a short current snapshot only when appropriate. See DIAGNOSTICS_OPERATIONS.md for qualified interpreter paths, interpretation and backup verification levels.
