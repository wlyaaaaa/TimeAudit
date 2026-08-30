# Local Runtime Files

This repository keeps a few baseline configuration files in Git, but some of them are mutated by local collectors while TimeAudit is running.

## Local-only state

- `tmp/`: one-off reboot checks, post-run notes, and local diagnostic scratch files.
- `.venv/`: the project-local Python 3.11 runtime created by `setup_runtime.ps1`.
- `LibreHardwareMonitor.config`: tracked as a baseline file, but this machine's running LibreHardwareMonitor process updates sensor/runtime state frequently.

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
