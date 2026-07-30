# PCConfig anomaly digest contract

TimeAudit owns telemetry collection, database semantics, thresholds, and the read-only aggregate provider `pcconfig_anomaly_digest.py`. PCConfig may consume this provider incrementally, but it must not query TimeAudit process/activity payloads directly or reinterpret raw rows.

## Interface

```powershell
python E:\Projects\Tools\TimeAudit\pcconfig_anomaly_digest.py `
  --after-utc <exclusive-UTC-cursor> `
  --until-utc <inclusive-UTC-bound>
```

The provider uses the existing `audit-postgres` container's local PostgreSQL socket through `docker exec`; it does not read, print, copy, or require a database password in the caller.

Successful output is `timeaudit.pcconfig-anomaly-digest.v1` and contains:

- TimeAudit owner/profile identity;
- a bounded window and next exclusive timestamp cursor;
- sample count and coverage freshness;
- aggregate anomaly IDs, severity, occurrence count, first/last time, and threshold reference;
- whether the anomaly recommends one PCConfig stable-configuration recheck;
- explicit zero-payload privacy flags.

It never returns raw telemetry values or rows, temperature/load series, process activity, window titles, network identifiers, credentials, or machine identifiers.

## Detection profile

`timeaudit:pcconfig-hardware-anomaly.v1` detects sustained CPU/GPU thermal pressure, memory pressure, disk-latency pressure, bounded user-space scheduler-jitter saturation, physical-bound telemetry errors, and source gaps. Scheduler jitter is not presented as real kernel DPC latency and does not recommend a stable-configuration recheck. Thresholds and minimum sample counts are TimeAudit semantics. They are anomaly signals, not proof that stable hardware/configuration changed.

The query window is `(after_utc, until_utc]`, must be at most 168 hours, and uses only aggregate filters over indexed `fact_system_hardware.timestamp`. A successful response advances the cursor to `until_utc`, including an empty window. Missing Docker/PostgreSQL or invalid output returns bounded `status=unavailable`; it does not restart services.

## PCConfig boundary

PCConfig may persist only its cursor and a bounded digest/decision receipt. An anomaly may trigger `Invoke-StableMachineProjection.ps1 -Action Inspect`; only the PCConfig live stable provider can decide that a new stable projection version is warranted. TimeAudit anomalies never enter `stable_machine_projection.json`, never cause an automatic hardware change, and do not require PersonalOS.

## Verification

```powershell
python -m unittest -v test_pcconfig_anomaly_digest.py
python pcconfig_anomaly_digest.py --after-utc <UTC> --until-utc <UTC>
```
