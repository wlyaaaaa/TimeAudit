# Local Runtime Files

This repository keeps a few baseline configuration files in Git, but some of them are mutated by local collectors while TimeAudit is running.

## Local-only state

- `tmp/`: one-off reboot checks, post-run notes, and local diagnostic scratch files.
- `.venv/`: the project-local Python 3.11 runtime created by `setup_runtime.ps1`.
- `LibreHardwareMonitor.config`: tracked as a baseline file, but this machine's running LibreHardwareMonitor process updates sensor/runtime state frequently.

The runtime owner is the Windows scheduled task `LibreHardwareMonitor` (with
`telemetry_watchdog.ps1` as its recovery path). TimeAudit's Python worker is a
read-only HTTP consumer; it must not launch a second `LibreHardwareMonitor.exe`.

## Local handling

On this workstation, `LibreHardwareMonitor.config` is marked with Git `skip-worktree` after the public ignore-rule update is committed. This keeps local runtime noise out of routine push audits while preserving the tracked baseline in the repository.

To intentionally update the baseline later:

```powershell
git update-index --no-skip-worktree LibreHardwareMonitor.config
git status -sb
```

Review the diff carefully before committing.

Rebuild the isolated telemetry runtime with:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup_runtime.ps1
```


## Diagnostic reliability state

`log/ahk_health.json`, `log/watchdog_outcome.json`, `log/diagnostics_activation.json`
and `log/restore_check.json` are local runtime evidence, not source/public
attachments. `log/restore_check.lock` is an OS-locked coordination file. Atomic
`log/buffer.csv.ahk.*.processing` segments belong to the existing ingester; a
`.tmp` file is not a committed event segment. Do not delete pending segments to
make health look green.

Completed database archives use `.dump` and `.dump.json`; `.partial` is not a
completed backup. Ordinary health reads metadata, while a restore drill uses a
separately identified disposable database. See `DIAGNOSTICS_OPERATIONS.md`.

Previously tracked/published historical diagnostic archives remain historical
source artifacts, not live evidence. This maintenance inspected only structural aggregates and did not disclose
their payloads, rewrite Git history or delete originals. New runtime receipts, private
records and temporary tests must not be added beside them. Test dependencies and
fixtures belong to the current E-drive task temp root, not the source tree.

Structural classification (2026-09-17): the historical PresentMon CSV contains 1,929 records and 28 columns, including a process identifier column; it is an old runtime artifact, not a test oracle. The UTF-16 text archive contains 1,315 lines and is not a parseable Python module; it is not active source. Neither file is consumed by current runtime or diagnostic entry points. Originals and their Git history are preserved.
