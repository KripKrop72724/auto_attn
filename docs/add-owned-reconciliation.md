# ADD-owned reconciliation (Zone Lite 2.3.0)

## Truth and safety contract

- The ZKT terminal is the attendance source of truth. Oracle is append-only; this workflow never deletes or replaces Oracle rows.
- ADD owns every job, source checkpoint, manifest row, retry, and operator action. The ESP never needs free flash to remember a command or a full-history checkpoint.
- A source certificate proves a contiguous terminal ordinal range was durably captured by ADD. It may be issued with explicit identity-blocked or quarantined exceptions.
- An Oracle membership certificate is separate and is issued only after every resolvable event in that source range is confirmed in Oracle. Identity-blocked rows remain fail-closed and visible.
- Only one terminal source scan runs nationwide at a time. Live punches and current-tail delivery have priority over full history.

## Operator workflow

1. Open **Reconciliation** in ADD and select one terminal.
2. Preflight must verify signed Zone Lite 2.3.0 or newer, `history_stream_v1`, range-resume support, connected ESP, stable certified ZKT, complete identity snapshot, and no active ZKT command or administrator lease.
3. Select **New complete reconcile**, enter an audited reason, administrator password, and the exact confirmation shown by ADD.
4. ADD anchors terminal serial, generation, cutoff count, record size, source size, and first-record digest.
5. Zone Lite reads at most the assigned bounded range. Each raw terminal row is hashed, encrypted at rest by ADD, and committed with a chained chunk digest before ADD acknowledges the next ordinal.
6. After any disconnect, reboot, power loss, or ADD restart, ADD reoffers the same committed ordinal. Firmware rechecks the first-record anchor and the last committed raw-record boundary before reading forward.
7. ADD seals source coverage only when every ordinal through the cutoff exists in the manifest and the final chain matches.
8. ADD drains events to Oracle with live and current-tail priority. Membership is checked in batches and the job closes only when resolvable membership is complete.

## Runtime resource policy

- Full history is request-only and globally serialized.
- Firmware does not allocate the multi-megabyte terminal dump. It reads a bounded range from `CMD_READ_BUFFER_CHUNK` and releases the prepared terminal buffer after the step.
- The ADD ORDS backlog applies hysteretic backpressure: a full-history scan pauses at the high watermark and resumes below the low watermark.
- ORDS delivery order is live, current reconcile, then full history. Full-history candidates are round-robin across connectors.
- Once ADD acknowledges a complete source certificate, firmware persists the certified cutoff and retires legacy historical/full-dump sweeps.
- The normal audit then examines only new append-tail ordinals in bounded groups, waits for ADD acknowledgement, and advances its NVS cursor. A terminal count regression invalidates coverage fail-closed.

## Failure semantics

| Condition | Result |
| --- | --- |
| ESP/ADD disconnect or reboot | No checkpoint advance; ADD reoffers the committed ordinal |
| ZKT command or administrator lease active | Job waits; it never forces a reboot or overlaps the exclusive operation |
| Terminal serial, count, first anchor, or committed boundary changes | Coverage/job safety hold; operator review required |
| ORDS unavailable or backlogged | Source capture pauses or Oracle assurance waits; live delivery retains priority |
| CNIC/identity unresolved | Attendance remains `BLOCKED_IDENTITY`; source coverage can seal with the explicit exception |
| Raw terminal record malformed or time invalid | Encrypted raw evidence is quarantined; Oracle membership cannot be certified silently |
| ESP preservation storage full/unavailable | ADD commands still execute from the durable control-plane offer; storage is never auto-formatted |
| Operator pause/cancel | Committed evidence is retained; no terminal or Oracle record is removed |

## Production release gates

Deploy ADD first with the feature dark, then enable the ADD feature flag only after migration, readiness, and UI verification. Firmware promotion requires a clean build, signed immutable image, exact SHA, and successful hardware-in-loop range-resume, disconnect, power-cycle, storage-pressure, live-punch, and rollback checks.

Roll out one zone at a time in this order:

1. SLICTOWER
2. Karachi
3. Swat
4. Peshawar

For every zone, require deployment `SUCCEEDED`, exact signed 2.3.0, ESP online/connected/OTA-ready, activity `ONLINE` or `LIVE_CAPTURE`, ZKT online/certified/snapshot-complete, non-regressed terminal counts, live attendance parity, zero unsafe resolvable rows, and no post-boot truth or reconcile failure. Hold the campaign and stop before the next zone on any failed gate.
