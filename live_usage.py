"""Read the evidence the live usage page needs, without rewriting old activity rows."""
from datetime import datetime, timezone
import json
import subprocess

def read_focus(query, start, end, grafana_sql):
    # Use the Grafana panel's CTEs as the single definition. Public and owner
    # page reads need no window title; return coverage even with zero sessions.
    core, boundary, _ = grafana_sql.partition("\n-- TimeAudit live focus result\n")
    if not boundary:
        raise ValueError("usage_focus_source_requires_updated_dashboard")
    core = core.replace("$__timeFrom()", ":'after_utc'").replace("$__timeTo()", ":'until_utc'")
    return query(core + """
SELECT jsonb_build_object(
  'min_seconds', (SELECT min_seconds FROM focus_contract),
  'recorded_to', (SELECT max(e) FROM clipped WHERE app NOT IN ('unknown','idle','system_collectiongap')),
  'rows', COALESCE((SELECT jsonb_agg(s ORDER BY s."from" DESC) FROM
    (SELECT "from", "to", app, seconds FROM focus_rows) s), '[]'::jsonb));
""", start, end)[0]


def _windows_power_events(start, end):
    def utc(t):
        return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    script = r"""
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$ErrorActionPreference='Stop'
$q="*[System[TimeCreated[@SystemTime >= 'START' and @SystemTime <= 'END'] and ((Provider[@Name='Microsoft-Windows-Kernel-General'] and (EventID=12 or EventID=13)) or (Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=41))]]"
try { $events=@(Get-WinEvent -LogName System -FilterXPath $q) }
catch { if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { $events=@() } else { throw } }
ConvertTo-Json -Compress -InputObject @(foreach ($e in $events) {
  $name = switch ($e.Id) { 12 {'StartTime'} 13 {'StopTime'} }
  $at = (([xml]$e.ToXml()).Event.EventData.Data | Where-Object Name -eq $name).InnerText
  @{id=$e.Id; at=($at ?? $e.TimeCreated.ToUniversalTime().ToString('o'))}
})
""".replace('START', utc(start)).replace('END', utc(end))
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
                            check=True, capture_output=True, encoding="utf-8", timeout=15,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return json.loads(result.stdout)


def read_power(start, end, read=_windows_power_events):
    """A matched kernel stop/start is a span; an unmatched startup is a point."""
    try:
        events = sorted({(datetime.fromisoformat(r["at"].replace("Z", "+00:00")), r["id"])
                         for r in read(start, end)})
    except (OSError, subprocess.SubprocessError, ValueError):
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
    return dict(spans=spans, markers=markers)


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
