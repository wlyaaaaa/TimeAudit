"""Read the evidence the live usage page needs, without rewriting old activity rows."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess


def read_idle_threshold_seconds():
    """Read the configured keyboard/mouse idle threshold from the collector."""
    try:
        source = Path(__file__).with_name('TimeAudit.ahk').read_text('utf-8-sig')
    except (OSError, UnicodeError):
        return None
    match = re.search(r'^[ \t]*global[ \t]+maxIdleTime[ \t]*:=[ \t]*([1-9][0-9]*)[ \t]*(?:;[^\n]*)?$', source, re.MULTILINE)
    return int(match[1]) / 1000 if match else None

def read_focus(query, start, end, grafana_sql):
    # Use the Grafana panel's CTEs as the single definition. Public and owner
    # page reads need no window title; return coverage even with zero sessions.
    core, boundary, _ = grafana_sql.partition("\n-- TimeAudit live focus result\n")
    if not boundary:
        raise ValueError("usage_focus_source_requires_updated_dashboard")
    core = core.replace("$__timeFrom()", ":'after_utc'").replace("$__timeTo()", ":'until_utc'")
    return query(core + """
SELECT jsonb_build_object(
  'rule', (SELECT to_jsonb(focus_contract) FROM focus_contract),
  'recorded_to', (SELECT max(e) FROM clipped WHERE app NOT IN ('unknown','idle','system_collectiongap')),
  'rows', COALESCE((SELECT jsonb_agg(s ORDER BY s."from" DESC) FROM
    (SELECT "from", "to", seconds, programs FROM focus_rows) s), '[]'::jsonb));
""", start, end)[0]


def _windows_power_events(start, end):
    def utc(t):
        return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    script = r"""
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$ErrorActionPreference='Stop'
$q="*[System[TimeCreated[@SystemTime >= 'START' and @SystemTime <= 'END'] and ((Provider[@Name='Microsoft-Windows-Kernel-General'] and (EventID=12 or EventID=13)) or (Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=41 or EventID=42 or EventID=107 or EventID=506 or EventID=507)) or (Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1))]]"
try { $events=@(Get-WinEvent -LogName System -FilterXPath $q) }
catch { if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { $events=@() } else { throw } }
try { $oldest=Get-WinEvent -LogName System -Oldest -MaxEvents 1 }
catch { if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { $oldest=$null } else { throw } }
$rows=@(foreach ($e in $events) {
  $data = ([xml]$e.ToXml()).Event.EventData.Data
  $name = switch ($e.Id) { 12 {'StartTime'} 13 {'StopTime'} }
  $at = ($data | Where-Object Name -eq $name).InnerText
  $row = @{id=$e.Id; at=($at ?? $e.TimeCreated.ToUniversalTime().ToString('o'))}
  if ($e.Id -eq 1) {
    $row.sleep_at = ($data | Where-Object Name -eq 'SleepTime').InnerText
    $row.wake_at = ($data | Where-Object Name -eq 'WakeTime').InnerText
  }
  $row
})
$retained = if ($oldest) { $oldest.TimeCreated.ToUniversalTime().ToString('o') }
ConvertTo-Json -Compress -InputObject @{events=$rows; retained_from=$retained}
""".replace('START', utc(start)).replace('END', utc(end))
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
                            check=True, capture_output=True, encoding="utf-8", timeout=15,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return json.loads(result.stdout)


def _sleep_evidence(rows, events, start, end):
    exact, matched, pending, sleeps, wakes = [], [], {}, set(), set()
    left, right = start.timestamp(), end.timestamp()
    for row in rows:
        if row["id"] == 1:
            try:
                sleep, wake = (datetime.fromisoformat(row[k]) for k in ("sleep_at", "wake_at"))
                if sleep.tzinfo is None or wake.tzinfo is None:
                    raise ValueError("unqualified sleep timestamps")
                a, b = sleep.timestamp(), wake.timestamp()
                if not 0 < a < b:
                    raise ValueError("invalid recorded sleep bounds")
            except (KeyError, TypeError, ValueError):
                wakes.add(datetime.fromisoformat(row["at"]).timestamp())
            else:
                if left < b and a < right:
                    exact.append((a, b))
                    sleeps.add(a)
                    wakes.add(b)
    for at, identity in events:
        stamp = at.timestamp()
        if identity in (42, 506):
            pending[identity] = stamp
            sleeps.add(stamp)
        elif identity in (107, 507):
            a = pending.pop({107: 42, 507: 506}[identity], None)
            if a is not None and a < stamp:
                matched.append((a, stamp))
            wakes.add(stamp)
        elif identity in (12, 13, 41):
            pending.clear()
    intervals = exact + [(a, b) for a, b in matched if not any(a < y and b > x for x, y in exact)]
    spans = []
    for a, b in sorted((max(a, left), min(b, right)) for a, b in intervals if a < right and b > left):
        if spans and a <= spans[-1][1]:
            spans[-1] = spans[-1][0], max(b, spans[-1][1])
        else:
            spans.append((a, b))
    return dict(sleep_spans=spans, sleep_points=sorted(t for t in sleeps if left <= t <= right),
                wake_points=sorted(t for t in wakes if left <= t <= right))


def read_power(start, end, read=_windows_power_events):
    """Return shutdown and sleep evidence, or None when the System log cannot be read."""
    try:
        data = read(start, end)
        rows = data["events"]
        retained = datetime.fromisoformat(data["retained_from"]) if data["retained_from"] else None
        events = sorted({(datetime.fromisoformat(r["at"].replace("Z", "+00:00")), r["id"])
                         for r in rows})
        if (retained is not None and retained.tzinfo is None) or any(at.tzinfo is None for at, _ in events):
            return None
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError):
        return None
    spans, markers, stop, boot = [], [], None, None
    for at, identity in events:
        if not start <= at <= end:
            continue
        if identity == 13:
            stop = at
        elif identity == 41:
            # Kernel-Power reports a restart, usually just after its kernel start event.
            if boot is None or (at - boot).total_seconds() > 120:
                markers.append(at.timestamp())
                stop, boot = None, at
            elif spans and spans[-1][1] == boot.timestamp():
                # The last stop was logged, but the following boot refutes a clean shutdown.
                spans.pop()
                markers.append(boot.timestamp())
        elif identity == 12:
            if stop is not None and stop < at:
                spans.append((stop.timestamp(), at.timestamp()))
            else:
                markers.append(at.timestamp())
            stop, boot = None, at
    coverage = dict(status="complete" if retained is not None and retained <= start else "retention_insufficient",
                    retained_from=retained.timestamp() if retained is not None else None,
                    window_from=start.timestamp(), window_to=end.timestamp())
    return dict(spans=spans, markers=markers, coverage=coverage, **_sleep_evidence(rows, events, start, end))


def apply_power(segments, power):
    """Use shutdowns and restart boundaries to label unsampled time."""
    if not power:
        return segments
    spans = list(power["spans"])
    for previous, segment in zip(segments, segments[1:]):
        if not segment.get("unrecorded") or previous.get("unrecorded") or previous["end"] != segment["start"]:
            continue
        restart = next((at for at in power["markers"] if segment["start"] < at <= segment["end"]), None)
        if restart is not None:
            spans.append((segment["start"], restart))
    if not spans:
        return segments
    spans.sort()
    result, i = [], 0
    for segment in segments:
        start, end = segment["start"], segment["end"]
        while i < len(spans) and spans[i][1] <= start:
            i += 1
        if segment["state"] is not None:
            result.append(segment)
            continue
        for left, right in spans[i:]:
            if left >= end:
                break
            a, b = max(start, left), min(end, right)
            if a >= b:
                continue
            if a > start:
                result.append(dict(segment, start=start, end=a))
            result.append(dict(start=a, end=b, state="关机离线"))
            start = b
        if start < end:
            result.append(dict(segment, start=start, end=end))
    return result
