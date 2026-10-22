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
- Every Zone Lite generation keeps a `source_coverage` push in a queue of one and applies it only at the top of its main loop, after the step that waits for an ACK returns. A push that arrives while the ESP waits for a tail or manifest ACK (for example the connect bootstrap's, when the tail or manifest was sent during the bootstrap) would be applied after that ACK and step the cursor back to the older value it carries, or drop a certificate the manifest ACK just installed. So whenever a session has sent a push that no positive tail or manifest ACK has followed yet, ADD sends its coverage as of the next positive tail ACK (committed or duplicate) or manifest ACK just *before* that ACK. The ESP handles frames in order, so the new push replaces the queued one before the ACK releases the waiting step, however the link delays either frame. It repeats what ADD already committed and certifies nothing.
- A tail answer's own push (an invalidation or a refusal below) is also sent before its error ACK, so the ESP has queued it before it schedules the next tail.

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
| Terminal moved from another connector that still owns its earlier punches | The baseline links each replayed punch to the retained row only when its serial, user ID, device time and punch all match and the previous connector no longer reports, expects or has confirmed the terminal serial. Linked rows count as already present, the job records `SOURCE_REPLAY_RETAINED_ELSEWHERE`, and the rows stay with their owner: never rewritten, re-attributed, released or delivered through the new connector, and a ZKT custody occurrence bound to one is held as `SOURCE_ATTENDANCE_BINDING_UNVERIFIED`. Anything else rejects the chunk with a `SOURCE_REPLAY_*` reason (`SOURCE_REPLAY_OWNER_STILL_BOUND` until the old connector is re-pinned to its own terminal). |
| Tail replays a punch another connector owns | Coverage is invalidated as `SOURCE_TAIL_FOREIGN_OWNER`: tail ordinals follow the certified cutoff, so another owner means a second reader on the same terminal |
| Connector re-onboarded (new generation) while the previous generation's coverage is still active and tailing | A new job runs in the connector's generation. Each scanned chunk is compared with the previous generation's ledger before anything is ingested, like a same-epoch re-read: different raw bytes start the fresh-read probes and, once confirmed, ADD's source-change recovery (recovery epoch, old coverage `SUPERSEDED_BY_CONFIRMED_SOURCE_MUTATION`, `review_required`); identical bytes with another record key or occurrence hold `SOURCE_DERIVED_KEY_DIVERGED`. When the job seals, ADD compares both ledgers again over every ordinal they share (ordinal, raw digest, record size, record key, occurrence; every canonical source kind including a recovery epoch's `RECOVERY_PREFIX` rows) and checks that the old ledger holds every ordinal its coverage committed. A match certifies the job, marks the old coverage `SUPERSEDED_BY_NEW_GENERATION_CERTIFICATE` and its epoch `SUPERSEDED` in one transaction, and seals the old generation's identifiers, cursors, chains and lineage digest in the certificate (`superseded_generation`, outcome `SUPERSEDED`). Preflight shows coverage generation next to connector generation. |
| The terminal re-reads, as the new generation's tail, ordinals the old coverage committed after the cutoff | Each re-read record must equal the replaced ledger's row (raw digest, size, key, occurrence) before it is ingested, and every tail must report a terminal count that reaches the old committed cursor. A difference invalidates the new coverage as `SOURCE_TAIL_LINEAGE_DIVERGED` (a lower count: `SOURCE_TAIL_COUNT_REGRESSION`; both CRITICAL `ADD_SOURCE_COVERAGE_INVALIDATED`) and nothing from that tail is ingested. The window belongs to the new epoch: every later full reconcile and coverage in it is compared with the replaced ledger too, even after the replacing coverage was itself replaced or invalidated, and a changed record is confirmed with fresh reads. Identical records add no attendance or Oracle row. |
| The old coverage fails on its own evidence while the new job's ledger is complete | Missing or malformed old ledger rows, no binding to this terminal's epoch, or a manifest terminal count below the old committed cursor (the firmware reads it just before sending the manifest): the old coverage is invalidated with its reason (`PREVIOUS_LEDGER_INCOMPLETE`, `PREVIOUS_BINDING_UNPROVEN`, `TERMINAL_ATTENDANCE_COUNT_REGRESSED`) and a latched CRITICAL `ADD_SOURCE_COVERAGE_INVALIDATED`, and the job certifies its own complete ledger (outcome `PREVIOUS_COVERAGE_INVALIDATED`). Where old rows exist, the re-read tail is still compared with them. |
| The new generation read different raw history at sealing (a job scanned before this check existed) | The job holds `SOURCE_GENERATION_LINEAGE_DIVERGED` (identical bytes with another key: `SOURCE_GENERATION_KEY_DIVERGED`) and raises the latched CRITICAL `ADD_SOURCE_GENERATION_LINEAGE_DIVERGED` with the ordinal and both digests. Nothing is replaced and the old coverage keeps tailing, so capture continues. Cancel, then start a new full reconcile: its chunk comparison sends the changed record through the fresh-read probes and source-change recovery. |
| Active coverage is from a newer generation than the job | The job holds `SOURCE_GENERATION_STALE`; newer coverage is never replaced by an older generation |
| A tail arrives from an older generation than ADD's active coverage | The chunk is not committed and is not a rejected message: ADD answers `SOURCE_TAIL_STALE_GENERATION` and pushes its current coverage, which the device adopts (`ADD_SOURCE_CHECKPOINT_REPLAY`). The connector is not marked degraded and no open rejection is cleared. If stale tails continue for 10 minutes since the coverage last advanced, ADD raises `ADD_SOURCE_TAIL_STALE_GENERATION` (HIGH, no health effect), which clears when a current-generation tail commits. |
| Sealing held on source-coverage evidence (`SOURCE_COVERAGE_*`, `SOURCE_GENERATION_*`) | A plain or guarded Retry, a Resume, or a Pause followed by Resume is refused while a read-only re-check of the sealed checkpoint predicts the same hold; it is accepted once the evidence changed and the device re-sends its manifest without a rescan. Employee repair does not wait on such a job (`SOURCE_DEPENDENCY_UNAVAILABLE`). The operator text says "Cancelling is safe" only where the active coverage was verified or the fault is the job's own; when the retained coverage's own receipts fail, ADD raises a latched CRITICAL `ADD_SOURCE_COVERAGE_UNVERIFIED` that outlives a Cancel. Preflight lists any active or held job as a blocker. |
| Guarded retry after the connector re-onboarded | `terminal_generation` pins the job's own generation. Once the connector's generation differs from it, the request must also pin `connector_generation`, so a re-onboard or token rotation after review is never passed silently. |
| Timestamp correction of a record ADD already corrected under another manifest row | A recovery epoch or a new generation holds the same terminal record under a new manifest row. A correction for any canonical row with the same terminal, serial, ordinal and raw digest counts: the new one is skipped as `ALREADY_CORRECTED` and no second derived punch is delivered. Review the new copy to close its job's exception gate. |
| Device message rejected as `EVIDENCE_INVALID` | The rejection alert and device log carry a bounded reason code and the ADD code location (`module:function:line`), never message text or payload values; an uncoded `ValueError` is reported as `UNCLASSIFIED_VALUE_ERROR` with its location |
| A tail extends a certificate ADD no longer holds, or holds under another epoch | ADD does not reject the message. A rejection would leave the ESP's cursor as it was, and it would resend the same range every tail interval. ADD answers with the same `source_coverage` push a reconnect sends, followed by an error ACK. With no active coverage the code is `SOURCE_TAIL_COVERAGE_INACTIVE`: the inactive placeholder withdraws the ESP's certificate, and a full reconcile certifies new coverage. With an active coverage that has a valid epoch for the same serial and generation, and a stale or missing required epoch, the code is `SOURCE_TAIL_EPOCH_STALE`: the ESP resumes from ADD's committed cursor, chain and epoch. A tail that itself invalidates coverage gets the inactive push in the same reply. No evidence is committed and coverage is not changed: the transaction keeps only the message sequence (a replay of the same message is ACKed as a duplicate, which the ESP never reads as a tail ACK) and one WARN device log row with the code and the refused ordinals. A refusal does not clear an open `DEVICE_MESSAGE_REJECTED` alert; only an accepted tail does. |
| ESP preservation storage full/unavailable | ADD commands still execute from the durable control-plane offer; storage is never auto-formatted |
| Operator pause/cancel | Committed evidence is retained; no terminal or Oracle record is removed |

## Production release gates

Deploy ADD first with the feature dark, then enable the ADD feature flag only after migration, readiness, and UI verification. Firmware promotion requires a clean build, signed immutable image, exact SHA, and successful hardware-in-loop range-resume, disconnect, power-cycle, storage-pressure, live-punch, and rollback checks.

Employee-scoped identity correction is a separate, explicit workflow under **Attendance → Needs review**. It may depend on this full-device source scan, but it never changes this workflow's manifests, event UIDs, physical punch facts, or disabled Oracle delete paths. Oracle UID membership and repaired identity-content proof remain separate. See [Attendance blocked-punch review and ORDS release runbook](attendance-repair-runbook.md).

Firmware still rolls out one zone at a time in this order; durable source reconciliation may run in parallel after the eligible devices are safely booted:

1. SLICTOWER
2. Karachi
3. Swat
4. Peshawar

For every zone, require deployment `SUCCEEDED`, the exact signed release, ESP online/connected/OTA-ready, activity `ONLINE` or `LIVE_CAPTURE`, ZKT online/certified/snapshot-complete, non-regressed terminal counts, exact source-cursor parity and chain continuity, zero unsafe resolvable rows, and no post-boot source or reconcile failure. Attendance-row count is intentionally not compared with the raw terminal count when evidenced source exceptions or terminal duplicates exist. Hold the campaign and stop before the next zone on any failed gate.
