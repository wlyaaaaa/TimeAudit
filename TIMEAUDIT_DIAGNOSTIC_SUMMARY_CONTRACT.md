# TimeAudit diagnostic summary contract

TimeAudit owns telemetry collection and the read-only provider
`timeaudit_diagnostic_summary.py`. The provider supplies bounded historical
evidence for personal computer diagnosis; it does not diagnose causality and it
does not control the collector.

## Interface

```powershell
python E:\Projects\Tools\TimeAudit\timeaudit_diagnostic_summary.py `
  --after-utc <exclusive-UTC-bound> `
  --until-utc <inclusive-UTC-bound>

# Quick relative window
python E:\Projects\Tools\TimeAudit\timeaudit_diagnostic_summary.py --hours 3
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
  samples whose FPS and frame time agree within a bounded tolerance; rejected
  positive samples are counted separately instead of being treated as gameplay;
- unioned active, idle, display-off, lock and sleep durations, plus uncovered
  and cross-state-overlap seconds;
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

Missing Docker/PostgreSQL, a timeout, invalid arguments, or invalid/oversized
query output returns only a bounded `status=unavailable` JSON document and exit
code 2.

## Verification

```powershell
python -m unittest -v test_timeaudit_diagnostic_summary.py
python timeaudit_diagnostic_summary.py
```
