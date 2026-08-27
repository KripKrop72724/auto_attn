# ADD-owned reconciliation (Zone Lite 2.4.4)

ADD schedules up to six terminal source scans in parallel. A device owns only
one strictly serial slot, every acknowledged chunk remains restart-safe, and a
disconnect or device-specific safety hold releases capacity for another zone.
The global full-history ORDS backlog gate continues to pause all new source
intake before downstream storage can be overloaded.

The final assignment may contain fewer than 100 records. ADD calculates its
minimum safe grant from the remaining source range, so a 1–99 record tail can
always finish without being misreported as exhausted backlog credit. Raw source
changes enter independent fresh-buffer probes; stable changes create a new
preserved source epoch while transient reads resume from the existing cursor.

## Truth and safety contract

- The ZKT terminal is the attendance source of truth. Oracle is append-only; this workflow never deletes or replaces Oracle rows.
- ADD owns every job, source checkpoint, manifest row, retry, and operator action. The ESP never needs free flash to remember a command or a full-history checkpoint.
- A source certificate proves a contiguous terminal ordinal range was durably captured by ADD. Every ordinal has exactly one canonical disposition: `EVENT`, `BLOCKED_IDENTITY`, `INVALID_TIME`, `MALFORMED`, or `TERMINAL_DUPLICATE`.
- An Oracle membership certificate is separate and is issued only after every resolvable event in that source range is confirmed in Oracle. Identity-blocked rows remain fail-closed and visible.
- Oracle assurance classifies every durable event outcome as confirmed, identity-held, actively retryable, or terminal-review-required. Unknown and terminal quarantine states fail closed for review; they are never presented as an endlessly retryable queue.
- Up to six isolated terminal source scans run nationwide at once, with at most one strictly serial scan per device. Live punches and current-tail delivery have priority over full history.

## Operator workflow

1. Open **Reconciliation** in ADD and select one terminal.
2. Preflight must verify signed Zone Lite 2.3.0 or newer, `history_stream_v1`, range-resume support, connected ESP, stable certified ZKT, complete identity snapshot, and no active ZKT command or administrator lease.
3. Select **New complete reconcile**, enter an audited reason, administrator password, and the exact confirmation shown by ADD.
4. ADD anchors terminal serial, generation, cutoff count, record size, source size, and first-record digest.
5. Zone Lite reads at most the assigned bounded range. Each raw terminal row is hashed, encrypted at rest by ADD, and committed with a chained chunk digest before ADD acknowledges the next ordinal.
6. After any disconnect, reboot, power loss, or ADD restart, ADD reoffers the same committed ordinal. Firmware rechecks the first-record anchor and the last committed raw-record boundary before reading forward.
7. ADD seals source coverage only when every ordinal through the cutoff exists in the unified source ledger and the final chain matches.
8. ADD drains events to Oracle with live and current-tail priority. Membership is checked in batches and the job closes only when resolvable membership is complete.
9. New terminal ordinals use signed, bounded `source_tail_chunk` messages. Invalid or malformed rows are committed as successful fail-closed source dispositions, so valid punches behind them continue.
10. A certified baseline containing invalid or malformed rows remains held until every exact in-scope exception has an administrator-password-confirmed review. The final review resumes Oracle assurance automatically from the existing checkpoint; the excluded rows, capture certificate, quarantine count, and source chain never change.

## Runtime resource policy

- Full history is request-only and bounded to six parallel device slots. Each terminal remains strictly serial and resumes only from its ADD-committed checkpoint.
- Firmware does not allocate the multi-megabyte terminal dump. It reads a bounded range from `CMD_READ_BUFFER_CHUNK` and releases the prepared terminal buffer after the step.
- The ADD ORDS backlog applies hysteretic backpressure: a full-history scan pauses at the high watermark and resumes below the low watermark.
- ORDS delivery order is live, current reconcile, then full history. Full-history candidates are round-robin across connectors.
- Once ADD acknowledges a complete source certificate, firmware persists the certified cutoff and retires legacy historical/full-dump sweeps.
- The normal audit then examines only new append-tail ordinals in bounded groups, waits for an exact atomic ADD acknowledgement, and advances its NVS cursor. A terminal count regression invalidates coverage fail-closed.
- ADD's active source checkpoint overrides the ESP's local cursor after every reconnect. A local-ahead cursor is safely replayed from ADD; an absent or inactive certificate disables tail advancement.

## Failure semantics

| Condition | Result |
| --- | --- |
| ESP/ADD disconnect or reboot | No checkpoint advance; ADD reoffers the committed ordinal |
| ZKT command or administrator lease active | Job waits; it never forces a reboot or overlaps the exclusive operation |
| Terminal serial, count, first anchor, or committed boundary changes | Coverage/job safety hold; operator review required |
| ORDS unavailable or backlogged | Source capture pauses or Oracle assurance waits; live delivery retains priority |
| CNIC/identity unresolved | Attendance remains `BLOCKED_IDENTITY`; source coverage can seal with the explicit exception |
| Oracle delivery reaches a terminal quarantine or an unknown state | Job enters `NEEDS_ATTENTION` with a non-PII state/count breakdown; no Oracle certificate is issued and no row is changed or deleted |
| Raw terminal record malformed or time invalid | Encrypted raw evidence is committed to the source ledger, excluded from attendance/Oracle, and later ordinals continue. Final assurance resumes automatically only after every exception inside the certified cutoff is reviewed; newer tail exceptions are outside that job's gate. |
| ESP preservation storage full/unavailable | ADD commands still execute from the durable control-plane offer; storage is never auto-formatted |
| Operator pause/cancel | Committed evidence is retained; no terminal or Oracle record is removed |

## Production release gates

Deploy ADD first with the feature dark, then enable the ADD feature flag only after migration, readiness, and UI verification. Firmware promotion requires a clean build, signed immutable image, exact SHA, and successful hardware-in-loop range-resume, disconnect, power-cycle, storage-pressure, live-punch, and rollback checks.

Employee-scoped identity correction is a separate, explicit workflow under **Reconciliation → Employee repair**. It may depend on this full-device source scan, but it never changes this workflow's manifests, event UIDs, physical punch facts, or disabled Oracle delete paths. Oracle UID membership and repaired identity-content proof remain separate. See [Employee attendance repair and resync runbook](attendance-repair-runbook.md).

Firmware still rolls out one zone at a time in this order; durable source reconciliation may run in parallel after the eligible devices are safely booted:

1. SLICTOWER
2. Karachi
3. Swat
4. Peshawar

For every zone, require deployment `SUCCEEDED`, the exact signed release, ESP online/connected/OTA-ready, activity `ONLINE` or `LIVE_CAPTURE`, ZKT online/certified/snapshot-complete, non-regressed terminal counts, exact source-cursor parity and chain continuity, zero unsafe resolvable rows, and no post-boot source or reconcile failure. Attendance-row count is intentionally not compared with the raw terminal count when evidenced source exceptions or terminal duplicates exist. Hold the campaign and stop before the next zone on any failed gate.
