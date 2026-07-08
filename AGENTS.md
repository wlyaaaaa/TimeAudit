# TimeAudit Agent Notes

- Keep Windows startup batch files such as `start_all.bat` with CRLF line endings. LF-only `.bat` files can make `cmd.exe` misparse parenthesized blocks and leave `TimeAudit_AutoStart` stuck.
- Task Scheduler does not reliably inherit the interactive user PATH. Keep absolute paths and the explicit Docker/Git PATH bootstrap in `start_all.bat`.
- `telemetry_watchdog.ps1` is the recovery loop for `main.py` and `TimeAudit.ahk`; avoid turning one-shot autostart tasks into duplicate long-running collectors.
- After path or startup-script edits, run `python test_start_all_docker_bootstrap.py` and verify the relevant scheduled tasks point at the current project root.
