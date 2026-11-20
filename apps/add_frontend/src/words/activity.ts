// Device Activity words (02-words Appendix B): one plain sentence, a kind
// (Routine / Problem), a group and "Act" for every device log code, including
// the codes seen live. The paged Activity tab gets its sentences from the
// server (be/activity_words.py, 04-device DEV-1, a copy of this appendix);
// `describeLog()` here is used when /activity is missing (older backend,
// "Showing a simpler view.") and by the words tests. The stored level never
// changes: Routine / Problem is display only.
//
// Imported only from the device chunk (lazy), never from the first chunk.

import { GROUP_LABELS } from './problems'
import type { ActivityAct, ActivityGroup, ActivityKind } from './types'

export type LogLevel = 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'CRITICAL'

export interface LogVariant {
  sentence: string
  kind?: ActivityKind
  act?: ActivityAct
  next?: string
}

export interface LogWords {
  sentence: string
  kind: ActivityKind
  group: ActivityGroup
  act: ActivityAct
  next?: string
  /** A different sentence, kind or act at one level (B1 "INFO / WARN" rows). */
  byLevel?: Partial<Record<LogLevel, LogVariant>>
}

export const KIND_LABELS: Record<ActivityKind, string> = { routine: 'Routine', problem: 'Problem' }
export { GROUP_LABELS }

const TELL_IT = 'Tell IT.'
const MEMORY_REPAIR = 'Tell IT: the link box memory may need repair.'
const SAME_AS_SERIAL = 'If the machine was replaced, confirm the new machine in Tools › Advanced tools. If not, connect the right machine.'
const RESET_CHECK = "Ask the place whether the machine's records were cleared or it was reset."
const FULL_CHECK = 'Start a full check in Recover attendance.'

const L = (group: ActivityGroup, kind: ActivityKind, act: ActivityAct, sentence: string, next?: string, byLevel?: LogWords['byLevel']): LogWords => ({
  group, kind, act, sentence, ...(next ? { next } : {}), ...(byLevel ? { byLevel } : {}),
})
const step = (sentence: string) => L('memory', 'routine', 'no', `${sentence}.`)
const repairFailure = (sentence: string) => L('memory', 'problem', 'yes', sentence, TELL_IT)

export const LOG_WORDS: Record<string, LogWords> = {
  // B1. Codes seen live
  ZKT_TIME_SYNC_READBACK_FAILED: L('machine', 'routine', 'if_repeats', "Set the machine's clock but couldn't confirm it. Trying again in a minute.", "Set the machine's date and time by hand."),
  ZKT_CONNECTION_LOST: L('machine', 'routine', 'if_repeats', 'Lost the connection to the machine. Trying again.', 'Check the machine is switched on and its network cable is plugged in.'),
  ZKT_CREDENTIAL_POLICY_OK: L('people', 'routine', 'no', 'Checked: nobody can check in with a PIN or card on this machine (fingerprint or face only).'),
  ZKT_TIME_SYNC_OK: L('machine', 'routine', 'no', "Set the machine's clock to Pakistan time."),
  CURRENT_TAIL_SOURCE_EXCEPTIONS_COMMITTED: L('machine', 'routine', 'list', 'Checked the newest records; some unreadable records were set aside for checking. Later check-ins are not affected.', 'Review unreadable records in Recover attendance › Unreadable records.'),
  ADD_SOURCE_COVERAGE_APPLIED: L('machine', 'routine', 'no', 'Ready: the link box knows which records the dashboard already has.'),
  ORDS_LEGACY_LOCK_SLOW: L('memory', 'routine', 'no', 'The link box memory was slow while sending check-ins.'),
  IDENTITY_CATALOG_APPLIED: L('people', 'routine', 'no', 'Received the latest list of people from the dashboard; checking records again.'),
  HIK_POLL_STATUS: L('machine', 'routine', 'no', 'Checked the machine: all fine.', undefined, {
    WARN: { sentence: "Couldn't check the machine. Trying again.", act: 'if_repeats', next: 'Check the machine is on and its network cable is plugged in.' },
  }),
  ADD_RECONNECTED: L('link_box', 'routine', 'no', 'Reconnected to the dashboard; sending the list of people again.'),
  ADD_MESSAGE_PROCESSING_FAILED: L('link_box', 'routine', 'if_repeats', "The dashboard couldn't save some information from the link box. This is a dashboard problem, not the device; it will try again.", 'Tell IT the dashboard is struggling.'),
  ZKT_STABLE: L('machine', 'routine', 'no', 'The connection to the machine is steady again.'),
  ZKT_TIME_SYNC_DEFERRED: L('machine', 'routine', 'no', "Waiting for the correct internet time before setting the machine's clock. Trying again in a minute."),
  ORACLE_RECONCILE_DELEGATED_TO_ADD: L('hr_system', 'routine', 'no', 'Handed records to the dashboard to send to the HR system.'),
  ATTENDANCE_TIMESTAMP_QUARANTINED: L('machine', 'routine', 'list', "A record with an impossible date or time was set aside for checking. The machine's clock may be wrong.", "Check the machine's date and time, then open Recover attendance › Unreadable records."),
  SCHEDULED_RESTART: L('machine', 'routine', 'no', 'The machine restarted as planned.'),
  LIVE_DIRECT_ACK_FALLBACK_SUCCEEDED: L('memory', 'routine', 'no', "Couldn't save a check-in in the link box memory, but it reached the dashboard directly."),
  ADD_DIRECT_ACK_FALLBACK_SUCCEEDED: L('memory', 'routine', 'no', "Couldn't use the link box memory, so check-ins went straight to the dashboard. Delivered."),
  LIVE_LOCAL_STORAGE_RECOVERED: L('memory', 'routine', 'no', 'A check-in was rescued after a link box memory hiccup and delivered.'),
  HIK_CLOCK_SAMPLE: L('machine', 'routine', 'no', "Read the machine's clock.", undefined, {
    WARN: { sentence: "Couldn't read the machine's clock.", act: 'if_repeats' },
  }),
  CURRENT_TAIL_SOURCE_COMMITTED: L('machine', 'routine', 'no', 'The newest records are checked and confirmed.'),
  LIVE_OUTBOX_LOCK_RECOVERED: L('memory', 'routine', 'no', 'A check-in was briefly stuck in the link box; it was delivered.'),
  LIGHT_RECONCILE_OK: L('machine', 'routine', 'no', "Quick check: the machine's record count matches. All fine."),
  LIVE_EVENT_DURABILITY_DEFERRED: L('memory', 'routine', 'if_repeats', "A check-in couldn't be saved in the link box or sent to the dashboard. The next record check will recover it from the machine.", MEMORY_REPAIR),
  LIVE_DIRECT_ACK_FALLBACK_FAILED: L('memory', 'routine', 'if_repeats', "A check-in couldn't be saved or sent. The next record check will recover it from the machine.", MEMORY_REPAIR),
  ADD_DIRECT_ACK_FALLBACK_FAILED: L('memory', 'routine', 'if_repeats', "Couldn't use the link box memory, and sending straight to the dashboard failed. The next record check will recover these records from the machine.", MEMORY_REPAIR),
  STORAGE_REPAIR_STARTED: L('memory', 'routine', 'no', 'Started repairing the link box memory.'),
  ZKT_FRAME_SESSION_MISMATCH: L('machine', 'routine', 'no', 'The machine replied in an older format; it was accepted.'),
  ZKT_FRAME_OVERSIZE: L('machine', 'routine', 'no', 'The machine replied in an older format; it was accepted.'),
  ZKT_RECORD_SIZE_FALLBACK: L('machine', 'routine', 'no', 'The machine replied in an older format; it was accepted.'),
  STORAGE_REPAIR_EXCEPTION_REFUSED: L('software', 'routine', 'no', "This link box can't get the memory-repair update yet."),
  ORDS_DRAIN_STORAGE_BACKPRESSURE: L('memory', 'routine', 'if_repeats', 'Sending to the HR system paused because the link box memory was busy. Check-ins are kept safe.'),
  STORAGE_INVENTORY: L('memory', 'routine', 'no', 'Memory check: all files in the link box can be read.', undefined, {
    WARN: { sentence: 'Memory check: some files in the link box are damaged.', kind: 'problem', act: 'yes', next: MEMORY_REPAIR },
  }),
  STORAGE_REPAIR_CHECKED: step('Checked the link box memory'),
  TRUTH_IDENTITY_INCOMPLETE: L('people', 'routine', 'list', "Some records were not sent again to the HR system because some aren't matched to an employee.", 'Check People for anyone not matched to an employee (CNIC missing).'),
  STORAGE_REPAIR_DONE: L('memory', 'routine', 'no', 'Memory repair finished; everything can be read.', undefined, {
    WARN: { sentence: "Memory repair finished; some damaged spots couldn't be fixed.", act: 'if_repeats' },
    ERROR: { sentence: 'Memory repair failed.', kind: 'problem', act: 'yes', next: TELL_IT },
  }),
  IDENTITY_CATALOG_MEMORY_FALLBACK: L('people', 'routine', 'no', "The list of people is loaded but couldn't be saved in the link box memory; it will be saved after reconnecting."),
  ZKT_SESSION_REFRESHED: L('machine', 'routine', 'no', 'Reconnected to the machine on purpose after a record check.'),
  FULL_RECONCILE_FAILED: L('machine', 'routine', 'if_repeats', "Couldn't finish reading records from the machine. New check-ins still arrive; it will try again."),
  FULL_RECONCILE: L('machine', 'routine', 'no', 'Started reading all records straight from the machine (routine full check).'),
  ZKT_SERIAL_MISMATCH: L('machine', 'problem', 'yes', 'This link box is connected to a different machine than the one it was set up for. Its check-ins are paused.', SAME_AS_SERIAL),
  ADD_SOURCE_CHECKPOINT_RETRY: L('machine', 'routine', 'no', "A record-check step didn't finish; it will be repeated."),
  DEVICE_MESSAGE_REJECTED: L('link_box', 'problem', 'yes', 'The dashboard refused some information from the link box. The link box keeps it and will send it again.', 'If it keeps happening, tell IT (details below).'),
  STORAGE_REPAIR_GAP: step('Found a damaged spot in the link box memory'),
  HIK_LIGHT_RECONCILE: L('machine', 'routine', 'no', 'Checking records saved on the machine.'),
  BLOCKED_IDENTITY_RECOVERY_DEFERRED: L('software', 'problem', 'yes', "Old link box software is keeping unmatched check-ins it can't shrink. A software update is needed to clear it.", 'Plan the link box software update.'),
  ADD_SOURCE_COVERAGE_CERTIFIED: L('machine', 'routine', 'no', 'Every record on this machine is checked and confirmed. From now on only new records are checked.'),
  LEASE_REVOKED_LOCAL: L('people', 'routine', 'no', 'Temporary admin access on the machine has ended (removed automatically).'),
  CURRENT_TAIL_AUDIT_RETRY: L('machine', 'routine', 'no', 'The newest-records check paused; it will continue.'),
  CURRENT_TAIL_CHECKPOINT_RETRY: L('machine', 'routine', 'no', "The newest-records check didn't finish; it will be repeated."),
  STORAGE_FILE_UNREADABLE: L('memory', 'problem', 'yes', 'A file in the link box memory is damaged.', MEMORY_REPAIR),
  LEGACY_QUEUE_READ_FAILED: L('memory', 'routine', 'if_repeats', "Couldn't read part of the check-ins saved in the link box. Trying again.", 'Tell IT: memory repair may be needed.'),
  STORAGE_REPAIR_EXCEPTION_AREA_TEST: L('memory', 'routine', 'no', 'Memory test passed.', undefined, {
    ERROR: { sentence: 'Memory test failed.', kind: 'problem', act: 'yes', next: TELL_IT },
  }),
  STORAGE_REPAIR_RECLAIM_SHORT: step('Not enough room to free up damaged memory this time'),
  STORAGE_REPAIR_RECLAIM_EXPOSURE: step('Measured the damaged memory'),
  STORAGE_REPAIR_BLOCKED: step('Some damaged parts wait until there is room'),
  STORAGE_REPAIR_DEFERRED: step('Memory repair postponed to the next restart'),
  STORAGE_REPAIR_FULL: L('memory', 'routine', 'if_repeats', 'The memory is too full to finish the repair; it continues at the next restart.'),
  STORAGE_REPAIR_LEVEL2_SKIPPED: step('Skipped the deeper clean-up'),
  STORAGE_REPAIR_COMPACTED: step('Freed space in the link box memory'),

  // B2. Machine
  ZKT_TIME_SYNC_FAILED: L('machine', 'routine', 'if_repeats', "Couldn't set the machine's clock (the machine refused the new time). Trying again in a minute.", "Set the machine's date and time by hand."),
  ZKT_SERIAL_CHANGED: L('machine', 'problem', 'yes', 'A different machine answered while connecting, so the connection was refused for safety.', SAME_AS_SERIAL),
  ZKT_SERIAL_UNREADABLE: L('machine', 'routine', 'if_repeats', "The machine didn't give its serial number. Trying again."),
  ZKT_PREPARED_READ_FAILED: L('machine', 'routine', 'if_repeats', 'A read from the machine failed. Trying again.'),
  ZKT_SECURE_SESSION: L('machine', 'routine', 'no', 'This machine uses the newer secure connection; connected.'),
  ZKT_SECURE_KEY_READY: L('machine', 'routine', 'no', 'Prepared the secure key for this machine.'),
  ZKT_SECURE_KEY_FAILED: L('machine', 'problem', 'if_repeats', "Couldn't prepare the secure key for this machine, so it can't connect. It tries again after a restart.", 'Ask someone at the place to unplug the link box for 10 seconds; if it stays, tell IT.'),
  ZKT_SECURE_HANDSHAKE_FAILED: L('machine', 'routine', 'if_repeats', 'The secure connection to the machine failed. Trying again.'),
  HIK_ANCHOR_MISMATCH: L('machine', 'problem', 'yes', "The machine's saved records have changed (for example after a reset), so it needs to be checked.", 'Ask the place whether the machine was reset or replaced, then run Recover attendance.'),
  HIK_HISTORY_PAGE_ACCEPTED: L('machine', 'routine', 'no', 'Sent a page of older records to the dashboard.'),
  HIK_HISTORY_RECEIPT_PENDING: L('machine', 'routine', 'no', 'Sent older records; waiting for the dashboard to confirm.'),
  HIK_HISTORY_RETRY: L('machine', 'routine', 'if_repeats', "Couldn't fetch older records from the machine. Trying again."),
  HIK_IP_REDISCOVERED: L('machine', 'routine', 'no', 'Found the machine at a new network address and saved it.'),
  HIK_IP_SAVE_PENDING: L('machine', 'routine', 'if_repeats', "Found the machine at a new address but couldn't save it yet; still using the old one."),
  HIK_LIVE_STREAM_CONNECTED: L('machine', 'routine', 'no', 'The live check-in feed from the machine is connected.'),
  HIK_LIVE_STREAM_RETRY: L('machine', 'routine', 'if_repeats', 'The live check-in feed dropped. Reconnecting; no check-ins are lost.'),
  HIK_WORKER_START_PENDING: L('link_box', 'routine', 'if_repeats', 'The link box is still starting up. Trying again.'),
  FULL_RECONCILE_DEFERRED_OUTBOX: L('machine', 'routine', 'no', 'Full check postponed until waiting check-ins are sent.'),
  FULL_RECONCILE_DEFERRED_ORDS_GATE: L('machine', 'routine', 'no', 'Full check postponed while check-ins are being sent to the HR system. It starts again shortly.'),
  TRUTH_READ_RETRY_SESSION: L('machine', 'routine', 'no', 'Reading records was interrupted; reconnecting to try again.'),
  TRUTH_READ_RETRY_EXHAUSTED: L('machine', 'routine', 'if_repeats', "Couldn't read the machine's records after several tries. New check-ins still arrive; it will try again later."),
  TRUTH_RETRY_COOLDOWN: L('machine', 'routine', 'if_repeats', 'Pausing record checks for 30 minutes after repeated failures. New check-ins still arrive.'),
  TRUTH_SNAPSHOT_IMPLAUSIBLE: L('machine', 'routine', 'if_repeats', 'The machine returned records with impossible dates, so this read was ignored. Trying again.', "Check the machine's date and time."),
  TRUTH_WINDOW_MEMORY_BOUNDED: L('machine', 'routine', 'no', 'Many records this month; checking them one day at a time.'),
  TRUTH_WINDOW_ALLOCATION_FAILED: L('link_box', 'routine', 'if_repeats', 'The link box ran out of working memory while checking records. It will try again.'),
  HISTORY_DUMP_CACHE_REUSED: L('machine', 'routine', 'no', 'Reused the records already read tonight instead of reading the machine again.'),
  HISTORY_DUMP_CACHE_VERIFIED: L('machine', 'routine', 'no', "Kept tonight's copy of the machine's records for the overnight check."),
  HISTORY_EMPTY_MONTHS_SKIPPED: L('machine', 'routine', 'no', 'Skipped months with no records.'),
  HISTORY_BACKFILL_COMPLETE: L('machine', 'routine', 'no', 'Finished checking older records.'),
  HISTORY_BACKFILL_BLOCKED: L('machine', 'problem', 'yes', "Finished checking older records, but some periods couldn't be checked.", 'Open Recover attendance for this device to see which periods.'),
  SOURCE_RANGE_READ_RETRY: L('machine', 'routine', 'no', 'A read from the machine was incomplete; nothing was changed. Trying again.'),
  SOURCE_RANGE_RELEASE_RETRY: L('machine', 'routine', 'no', "The machine didn't confirm the end of a read; reconnecting."),
  SOURCE_RANGE_SNAPSHOT_RETRY: L('machine', 'routine', 'no', 'New check-ins arrived while reading; reading again.'),
  SOURCE_RANGE_PREPARE_FAILED: L('machine', 'routine', 'if_repeats', "Couldn't start reading records from the machine; it will continue from where it stopped."),
  SOURCE_RANGE_LAYOUT_INVALID: L('machine', 'problem', 'yes', "The machine's records are in an unexpected format, so record checks stopped for safety.", 'Ask IT to review this machine in Recover attendance.'),
  SOURCE_RANGE_COUNT_REGRESSION: L('machine', 'problem', 'yes', 'The machine now holds fewer records than before (they may have been cleared). Record checks stopped for safety.', RESET_CHECK),
  SOURCE_FIRST_ANCHOR_DIVERGED: L('machine', 'problem', 'yes', "The machine's oldest saved record has changed (it may have been reset). Record checks stopped for safety.", RESET_CHECK),
  SOURCE_COMMITTED_BOUNDARY_DIVERGED: L('machine', 'problem', 'yes', "The machine's records changed where the last check ended (records don't match). Record checks stopped for safety.", RESET_CHECK),
  RECONCILE_TERMINAL_SERIAL_MISMATCH: L('machine', 'problem', 'yes', 'This record check was for a different machine than the one connected, so it was stopped.', 'Check that the right machine is connected.'),
  SOURCE_PROBE_ORDINAL_UNAVAILABLE: L('machine', 'routine', 'if_repeats', "A requested record isn't on the machine any more (it now has fewer records)."),
  ADD_SOURCE_COVERAGE_INVALIDATED: L('machine', 'problem', 'yes', 'The machine now has fewer records than were already confirmed (they may have been cleared). A full check is needed.', FULL_CHECK),
  ADD_SOURCE_COVERAGE_TERMINAL_MISMATCH: L('machine', 'problem', 'yes', 'The confirmed records belong to a different machine, so checking new records is paused.', 'Check that the right machine is connected, then start a full check in Recover attendance.'),
  ADD_SOURCE_CHECKPOINT_REPLAY: L('machine', 'routine', 'no', 'The link box was ahead of the dashboard; sending again from the last confirmed record.'),
  ASSIGNMENT_RELEASED_FOR_COMMAND: L('machine', 'routine', 'no', 'Paused the record check so your request could run right away.'),
  ATTENDANCE_TIMESTAMP_QUARANTINE_SUMMARY: L('machine', 'routine', 'list', 'Some records with impossible dates or times were set aside for checking.', 'Open Recover attendance › Unreadable records.'),
  LIVE_PACKET_FORMAT_REJECTED: L('machine', 'routine', 'if_repeats', 'A live check-in message from the machine was garbled; the next record check will recover the check-in.'),
  COMM_KEY_APPLIED: L('machine', 'routine', 'no', 'The machine password (COMM Key) was changed and works.'),
  SOURCE_TAIL_COVERAGE_INACTIVE: L('machine', 'routine', 'if_repeats', "The link box's newest-records check was out of date, so the dashboard paused it. A full check restarts it.", FULL_CHECK),
  SOURCE_TAIL_EPOCH_STALE: L('machine', 'routine', 'no', 'The link box used an old check position; the dashboard sent the current one and checking continues.'),

  // B2. HR system
  ORACLE_RECONCILE_ACCEPTED: L('hr_system', 'routine', 'no', 'The HR system accepted the records.'),
  ORDS_RECONCILE_BACKOFF: L('hr_system', 'routine', 'if_repeats', 'Waiting before sending records to the HR system again (it was busy or unreachable).'),
  ORDS_RECONCILE_WINDOW_LIMIT: L('hr_system', 'problem', 'yes', 'Too many records in one period to send in one go; skipped for now.', 'Tell IT; this period needs a manual send.'),
  ORDS_RECONCILE_PAYLOAD_FAILED: L('hr_system', 'problem', 'yes', "Couldn't prepare the records to send to the HR system.", TELL_IT),
  ORDS_RECONCILE_TRANSPORT_RETRY: L('hr_system', 'routine', 'if_repeats', "Couldn't reach the HR system; trying again."),
  ORDS_RECONCILE_TRANSPORT_RECOVERED: L('hr_system', 'routine', 'no', 'Reached the HR system again.'),
  ORDS_RECONCILE_ENDPOINT_MISSING: L('hr_system', 'problem', 'yes', "The HR system address the link box uses for record checks isn't answering.", 'Tell IT the HR system link for record checks is down.'),
  ORDS_RECONCILE_AUTH_FAILED: L('hr_system', 'problem', 'yes', "The HR system refused the link box's login.", 'Tell IT to check the HR system login the link boxes use.'),
  ORDS_RECONCILE_REJECTED: L('hr_system', 'problem', 'yes', 'The HR system rejected some records.', 'Tell IT (details below).'),
  ORDS_RECONCILE_RESPONSE_INVALID: L('hr_system', 'routine', 'if_repeats', "The HR system's answer was unclear; it will be checked again."),
  ORDS_RECONCILE_HTTP_FAILED: L('hr_system', 'routine', 'if_repeats', "Couldn't send the records to the HR system (network or server error). It will try again."),
  ADD_TRUTH_QUEUE_SATURATED: L('link_box', 'routine', 'if_repeats', "Too many check-ins were waiting, so part of this record check wasn't saved. New check-ins go first; the next check fixes it."),
  LOCAL_RECONCILE_STORAGE_RECOVERED: L('memory', 'routine', 'no', 'The link box memory was full, but every record still reached the dashboard. Nothing was deleted.'),

  // B2. People
  IDENTITY_CATALOG_STAGE_FAILED: L('people', 'routine', 'if_repeats', "Couldn't start loading the new list of people; the current list stays in use."),
  IDENTITY_CATALOG_CHUNK_REJECTED: L('people', 'routine', 'if_repeats', 'Part of the new list of people arrived broken; the current list stays in use. It will be sent again.'),
  IDENTITY_CATALOG_COMMIT_REJECTED: L('people', 'routine', 'if_repeats', 'The new list of people was incomplete; the current list stays in use.'),
  IDENTITY_CATALOG_PERSIST_FAILED: L('people', 'problem', 'yes', "Couldn't save the list of people in the link box, so some features stay paused until it is saved.", 'Tell IT: the link box memory may be full.'),
  USER_SNAPSHOT_TRUNCATED: L('people', 'problem', 'yes', 'The machine has too many people to read safely, so adding or changing people is turned off.', 'Remove people who have left from the machine, or tell IT.'),
  USER_SNAPSHOT_UNSTABLE: L('people', 'routine', 'no', "The machine's list of people changed while it was being read; reading again."),
  ZKT_CREDENTIAL_POLICY_FAILED: L('people', 'routine', 'if_repeats', "Couldn't remove PIN or card check-ins from the machine. Trying again."),
  ZKT_CREDENTIAL_IDENTITY_DEFERRED: L('people', 'routine', 'no', "The dashboard didn't confirm the list of people in time; it will be sent again."),
  LEASE_EVIDENCE_REQUIRED: L('people', 'problem', 'yes', "The record of temporary admin access can't be read, so changes to people on this machine are paused. Check-ins still work.", TELL_IT),
  LEASE_USER_MISSING: L('people', 'problem', 'yes', 'The person given temporary admin access is no longer on the machine.', 'Check People on this machine.'),
  LEASE_IDENTITY_CHANGED: L('people', 'problem', 'yes', "The temporary admin access doesn't match this machine; nothing was changed.", TELL_IT),
  LEASE_REVOKE_FAILED: L('people', 'problem', 'yes', "Couldn't remove temporary admin access from the machine. Trying again.", "If it doesn't clear, remove the admin rights at the machine."),

  // B2. Link box memory
  NVS_CHECKPOINT_FAILED: L('memory', 'problem', 'yes', "The link box couldn't read its saved progress; a full record check will be needed.", FULL_CHECK),
  SOURCE_INVALIDATION_NOT_PERSISTED: L('memory', 'problem', 'yes', "The machine's record count went down, and the link box couldn't save this. A full check is needed.", FULL_CHECK),
  ADD_SOURCE_COVERAGE_DEFERRED: L('memory', 'routine', 'if_repeats', "Couldn't save the last confirmed record in the link box memory. It will try again."),
  AUTHENTICATED_IP_NOT_PERSISTED: L('memory', 'routine', 'no', "Connected to the machine, but couldn't save its network address yet. It will save it later."),
  STORAGE_RECOVERY_SCAN: L('memory', 'routine', 'no', 'Found saved check-ins to rescue.'),
  STORAGE_RECOVERY_PROGRESS: L('memory', 'routine', 'no', 'Rescuing saved check-ins and sending them to the dashboard.'),
  STORAGE_RECOVERY_UNREADABLE_REGION: L('memory', 'routine', 'no', 'Skipped a damaged spot in the link box memory.'),
  STORAGE_RECOVERY_UNREADABLE_LIMIT: L('memory', 'problem', 'yes', 'Too much of the link box memory is damaged to rescue automatically. Nothing was removed.', TELL_IT),
  STORAGE_RECOVERY_CUSTODY_COMPLETE: L('memory', 'routine', 'no', 'Every readable check-in from the damaged memory is now safe in the dashboard.'),
  STORAGE_REPAIR_SPAN: step('Repair detail'),
  STORAGE_REPAIR_SIZE_RECOVERED: step("Fixed a file's size"),
  STORAGE_REPAIR_REBUILT: step('Rebuilt a file'),
  STORAGE_REPAIR_CATALOG_DEFERRED: step('The list of people will be removed and sent again later'),
  STORAGE_REPAIR_CATALOG_DROPPED: step('The list of people was removed; the dashboard sends it again'),
  STORAGE_REPAIR_CURSOR_RESET: step('Saved check-ins will be sent again from the start (copies are ignored)'),
  STORAGE_REPAIR_TAIL_TERMINATED: step('Cut off a damaged last line'),
  STORAGE_REPAIR_RESTORE_WAITING: step('Waiting for free space to put data back'),
  STORAGE_REPAIR_UNHIDDEN: step('Recovered a file the check had started deleting'),
  STORAGE_REPAIR_RESTORE_DEFERRED: step('Kept a file for the next restart'),
  STORAGE_REPAIR_MANIFEST_FULL: step('Repair notes are full'),
  STORAGE_REPAIR_RECORDS_FULL: step('Repair notes are full'),
  STORAGE_REPAIR_SUPERSEDED: step('A newer copy replaced an old one'),
  STORAGE_REPAIR_MERGE_DEFERRED: step('Waiting for room to merge files'),
  STORAGE_REPAIR_MERGED: step('Merged a saved file'),
  STORAGE_REPAIR_RESUME_BLOCKED: step("Couldn't resume an interrupted repair yet"),
  STORAGE_REPAIR_SIZE_TORN: step("A file's size couldn't be fixed; left as it is"),
  STORAGE_REPAIR_COMPACT_STOPPED: step('Tidying stopped; checking again'),
  STORAGE_REPAIR_RESERVE: step('Used reserved free space'),
  STORAGE_REPAIR_NOT_READY: step("A saved file isn't consistent yet"),
  STORAGE_REPAIR_NO_TIME: step('Not enough time left; continuing at the next restart'),
  STORAGE_REPAIR_PROBE_ALTERNATE: step('Testing the memory with a second file'),
  STORAGE_REPAIR_RECLAIM: step('Freed some damaged memory'),
  STORAGE_REPAIR_RECLAIM_REDONE: step('Finished a repair step after a restart'),
  STORAGE_REPAIR_COMMANDS_MERGED: step('Rebuilt the list of waiting requests'),
  STORAGE_REPAIR_RECLAIM_SETTLED: L('memory', 'routine', 'no', 'Finished putting back freed data.', undefined, {
    ERROR: { sentence: "Some freed data couldn't be put back.", kind: 'problem', act: 'yes', next: TELL_IT },
  }),
  STORAGE_REPAIR_RECLAIM_SOURCES: L('memory', 'routine', 'no', 'Waiting to read the backup area.', undefined, {
    ERROR: { sentence: "Memory repair couldn't load its backup copy; nothing was repaired.", kind: 'problem', act: 'yes', next: TELL_IT },
  }),
  STORAGE_REPAIR_RESTORE_RETRIED: L('memory', 'routine', 'no', 'Put saved files back.', undefined, {
    ERROR: { sentence: "Some saved files couldn't be put back.", kind: 'problem', act: 'yes', next: TELL_IT },
  }),
  STORAGE_REPAIR_MOUNT_FAILED: repairFailure("The link box memory wouldn't open; nothing was repaired."),
  STORAGE_REPAIR_JOURNAL_FAILED: repairFailure("Memory repair couldn't start (it couldn't save its notes)."),
  STORAGE_REPAIR_NVS_FULL: repairFailure('The link box settings memory is full, so the repair only reported this time.'),
  STORAGE_REPAIR_DISABLED: repairFailure('Memory repair is switched off on this link box after several failed tries.'),
  STORAGE_REPAIR_GC_UNSAFE: repairFailure('The memory is too full to tidy safely; new saves could damage files.'),
  STORAGE_REPAIR_CHECK_DELETED: repairFailure('The memory check removed a damaged file.'),
  STORAGE_REPAIR_MANIFEST_UNREADABLE: repairFailure("The notes from an interrupted repair can't be read."),
  STORAGE_REPAIR_RECLAIM_FAILED: repairFailure('A memory repair round failed; the memory stays closed until the next restart.'),
  STORAGE_REPAIR_RECLAIM_MISMATCH: repairFailure("Repaired files didn't match; the originals were put back."),
  STORAGE_REPAIR_RECLAIM_UNAVAILABLE: repairFailure("Damaged memory couldn't be freed; nothing changed."),
  STORAGE_REPAIR_COMMANDS_FAILED: repairFailure("Couldn't rebuild the list of waiting requests."),

  // B2. Software
  OTA_IMAGE_UNVERIFIED: L('software', 'routine', 'if_repeats', "The downloaded software update was damaged, so it wasn't installed. It will download again.", 'Pause this update and tell IT.'),
  STORAGE_REPAIR_EXCEPTION_ELIGIBLE: L('software', 'routine', 'no', 'This link box qualifies for the special memory-repair update.'),
  STORAGE_REPAIR_EXCEPTION_EVIDENCE: L('software', 'routine', 'no', 'Sent memory-repair details to the dashboard.'),
  STORAGE_REPAIR_EXCEPTION_CONFIRMED: L('software', 'routine', 'no', 'The special memory-repair update is confirmed.'),
  STORAGE_REPAIR_EXCEPTION_RESTART: L('software', 'routine', 'no', 'Restarting for memory repair.'),
  STORAGE_REPAIR_EXCEPTION_ROUND: L('software', 'routine', 'no', 'Memory repair round finished.', undefined, {
    ERROR: { sentence: 'Memory repair round failed.', kind: 'problem', act: 'yes', next: TELL_IT },
  }),
  STORAGE_REPAIR_EXCEPTION_RECORD: L('software', 'problem', 'yes', "The special repair couldn't save its progress note.", TELL_IT),
  STORAGE_REPAIR_EXCEPTION_WAITING: L('software', 'problem', 'if_repeats', 'The special repair is waiting; the link box memory stays closed until the next restart.', TELL_IT),
  STORAGE_REPAIR_EXCEPTION_DEPLOYMENT_ENDED: L('software', 'routine', 'no', 'The special memory-repair update has ended.'),
  STORAGE_REPAIR_EXCEPTION_UNHEALTHY: L('software', 'problem', 'yes', "The special memory repair didn't fix this link box.", 'Replace or re-install the link box (IT).'),
  STORAGE_REPAIR_EXCEPTION_UNREPAIRED: L('software', 'problem', 'yes', "The special memory repair didn't fix this link box.", 'Replace or re-install the link box (IT).'),
}

/** Engineer-test codes are formatted at run time; they all read the same. */
export const ENGINEER_TEST: LogWords = L('machine', 'routine', 'no', 'Engineer test finished; no settings were changed.')
const ENGINEER_TEST_PATTERN = /^ZKT_DIAG_/

// B3. Group from the subsystem, and the safe fallback by level.
export const SUBSYSTEM_GROUPS: Record<string, ActivityGroup> = {
  zkt: 'machine',
  hikvision: 'machine',
  reconcile: 'machine',
  zkt_diagnostic: 'machine',
  configuration: 'machine',
  attendance: 'machine',
  ords: 'hr_system',
  identity: 'people',
  users: 'people',
  enrollment: 'people',
  storage: 'memory',
  live: 'memory',
  ota: 'software',
}

export const LEVEL_FALLBACK: Record<LogLevel, LogVariant & { kind: ActivityKind; act: ActivityAct }> = {
  DEBUG: { sentence: 'Other activity from the link box.', kind: 'routine', act: 'no' },
  INFO: { sentence: 'Other activity from the link box.', kind: 'routine', act: 'no' },
  WARN: { sentence: 'The link box reported a small issue and is handling it.', kind: 'routine', act: 'if_repeats' },
  ERROR: { sentence: 'The link box reported a problem.', kind: 'problem', act: 'if_repeats' },
  CRITICAL: { sentence: 'The link box reported a serious problem.', kind: 'problem', act: 'yes', next: 'Tell IT (details below).' },
}

const normalizeLevel = (level: string | null | undefined): LogLevel => {
  const upper = (level ?? 'INFO').toUpperCase()
  if (upper === 'WARNING') return 'WARN'
  return (['DEBUG', 'INFO', 'WARN', 'ERROR', 'CRITICAL'] as const).find((value) => value === upper) ?? 'INFO'
}

// B4. Reason words inside HIK_POLL_STATUS / HIK_HISTORY_RETRY / HIK_LIVE_STREAM_RETRY.
export const HIK_REASON_WORDS: Record<string, string> = {
  ok: 'all fine',
  invalid_configuration: 'the link box settings for this machine are incomplete',
  network_timeout_or_unreachable: "the machine isn't answering on the network",
  authentication_rejected: 'the machine refused the password',
  http_error: 'the machine replied with an error',
  response_too_large: "the machine's reply was too large",
  identity_or_history_changed: "the machine's saved records changed",
  invalid_response: "the machine's reply didn't make sense",
  durable_storage_failed: "couldn't save check-ins in the link box",
  unknown: 'unknown reason',
}

// B4. ZKT_CONNECTION_LOST reasons (message text).
const CONNECTION_LOST: Array<[string, string]> = [
  ['established ZKT session disconnected', 'Lost the connection to the machine. Trying again.'],
  ['ZKT session ended before the stability window', 'The connection dropped right after connecting. Trying again.'],
  ['authenticated discovery did not find the assigned ZKT', "Can't find the machine on the network. Trying again."],
]

// B4. HIK_LIGHT_RECONCILE states.
const LIGHT_CHECK: Record<string, LogVariant> = {
  WAITING: { sentence: 'Record check waiting for its turn.' },
  SCANNING: { sentence: 'Checking records saved on the machine.' },
  WAITING_DELIVERY: { sentence: 'Record check paused until waiting check-ins are sent.' },
  WAITING_TERMINAL: { sentence: 'Record check paused until the machine answers.', act: 'if_repeats' },
  RETRYING: { sentence: 'Record check hit a problem; trying again.', act: 'if_repeats' },
  COMPLETE: { sentence: 'Finished checking records saved on the machine.' },
  BLOCKED: { sentence: 'The record check is stuck and needs IT.', kind: 'problem', act: 'yes', next: "Ask IT to look at this machine's record check." },
}

export interface LogRow {
  code?: string | null
  level?: string | null
  subsystem?: string | null
  message?: string | null
  context?: Record<string, unknown> | null
}

export interface DescribedLog {
  code: string
  sentence: string
  kind: ActivityKind
  group: ActivityGroup
  groupLabel: string
  act: ActivityAct
  next?: string
  /** True when the code has no words (B3 fallback used). */
  unknown: boolean
}

/** A number named `key` from `context` or a fixed `key=123` in the message. Never free text. */
export function readNumber(row: LogRow, key: string): number | null {
  const value = row.context?.[key]
  if (typeof value === 'number' && Number.isFinite(value)) return value
  const match = new RegExp(`(?:^|[;\\s,])${key}=(-?\\d+(?:\\.\\d+)?)(?![\\w.])`).exec(row.message ?? '')
  return match ? Number(match[1]) : null
}

const readWord = (row: LogRow, key: string): string | null => {
  const value = row.context?.[key]
  if (typeof value === 'string' && /^[a-z_]+$/.test(value)) return value
  const match = new RegExp(`(?:^|[;\\s,])${key}=([a-z_]+)(?![\\w])`).exec(row.message ?? '')
  return match ? match[1] : null
}

const countText = (count: number, one: string, many: string) => `${new Intl.NumberFormat('en-PK').format(count)} ${count === 1 ? one : many}`

/** Sentences that take a number read from the row (only numbers, never echoed text). */
function withNumbers(code: string, level: LogLevel, row: LogRow, base: LogVariant): LogVariant {
  switch (code) {
    case 'IDENTITY_CATALOG_APPLIED': {
      const rows = readNumber(row, 'rows')
      return rows === null ? base : { ...base, sentence: `Received the latest list of people (${new Intl.NumberFormat('en-PK').format(rows)}) from the dashboard; checking records again.` }
    }
    case 'CURRENT_TAIL_SOURCE_EXCEPTIONS_COMMITTED': {
      const match = /with (\d+) fail-closed source exception/.exec(row.message ?? '')
      const count = match ? Number(match[1]) : readNumber(row, 'exceptions')
      return count === null ? base : { ...base, sentence: `Checked the newest records; ${countText(count, 'unreadable record', 'unreadable records')} set aside for checking. Later check-ins are not affected.` }
    }
    case 'HIK_POLL_STATUS': {
      if (level === 'WARN') {
        const reason = readWord(row, 'reason')
        const words = reason ? HIK_REASON_WORDS[reason] : undefined
        const next = reason === 'authentication_rejected' ? 'Check the password saved for this machine.' : base.next
        return { ...base, sentence: words ? `Couldn't check the machine: ${words}. Trying again.` : base.sentence, next }
      }
      const queued = readNumber(row, 'queued')
      const storage = readWord(row, 'storage')
      let sentence = 'Checked the machine: all fine.'
      if (queued && queued > 0) sentence += ` ${countText(queued, 'check-in', 'check-ins')} waiting to be sent.`
      if (storage && storage !== 'verified') sentence += ' Link box memory not confirmed yet.'
      return { ...base, sentence }
    }
    case 'STORAGE_REPAIR_STARTED': {
      const used = readNumber(row, 'used_pct') ?? readNumber(row, 'percent')
      return used === null ? base : { ...base, sentence: `Started repairing the link box memory (${Math.round(used)}% full).` }
    }
    case 'ZKT_CONNECTION_LOST': {
      const found = CONNECTION_LOST.find(([text]) => (row.message ?? '').includes(text))
      return found ? { ...base, sentence: found[1] } : base
    }
    case 'HIK_LIGHT_RECONCILE': {
      const state = /state=([A-Z_]+)/.exec(row.message ?? '')?.[1] ?? (typeof row.context?.state === 'string' ? (row.context.state as string) : null)
      return state && LIGHT_CHECK[state] ? { ...base, ...LIGHT_CHECK[state] } : base
    }
    case 'FULL_RECONCILE': {
      return /historical=true/.test(row.message ?? '') || row.context?.historical === true
        ? { ...base, sentence: 'Started checking older records (overnight).' }
        : base
    }
    case 'FULL_RECONCILE_FAILED': {
      return /unsafe or rejected/.test(row.message ?? '')
        ? { ...base, sentence: "A record check stopped because some records couldn't be matched safely. Nothing was changed in the HR system; it will try again tonight.", act: 'list', next: 'Open Recover attendance for this device.' }
        : base
    }
    default:
      return base
  }
}

/** Words of one log code (and level), without reading the row. */
export function logWords(code: string | null | undefined, level?: string | null): LogWords | null {
  const key = (code ?? '').trim().toUpperCase()
  if (!key) return null
  const entry = LOG_WORDS[key] ?? (ENGINEER_TEST_PATTERN.test(key) ? ENGINEER_TEST : undefined)
  if (!entry) return null
  const variant = entry.byLevel?.[normalizeLevel(level)]
  return variant ? { ...entry, ...variant, byLevel: entry.byLevel } : entry
}

/**
 * One plain Activity row for a raw log row, when the server's /activity is not
 * available. Unknown codes fall back by level and subsystem (B3); never the
 * raw code or message as the sentence.
 */
export function describeLog(row: LogRow): DescribedLog {
  const code = (row.code ?? '').trim()
  const level = normalizeLevel(row.level)
  const words = logWords(code, level)
  if (words) {
    const filled = withNumbers(code.toUpperCase(), level, row, { sentence: words.sentence, kind: words.kind, act: words.act, next: words.next })
    return {
      code,
      sentence: filled.sentence,
      kind: filled.kind ?? words.kind,
      group: words.group,
      groupLabel: GROUP_LABELS[words.group],
      act: filled.act ?? words.act,
      ...(filled.next ? { next: filled.next } : {}),
      unknown: false,
    }
  }
  const fallback = LEVEL_FALLBACK[level]
  const group = SUBSYSTEM_GROUPS[(row.subsystem ?? '').toLowerCase()] ?? 'link_box'
  return {
    code,
    sentence: fallback.sentence,
    kind: fallback.kind,
    group,
    groupLabel: GROUP_LABELS[group],
    act: fallback.act,
    ...(fallback.next ? { next: fallback.next } : {}),
    unknown: true,
  }
}

/** Activity tab words (02-words §2 states). */
export const ACTIVITY_TAB = {
  tab: 'Activity',
  ariaLabel: 'Device activity',
  heading: 'What this device did recently',
  simpler: 'Showing a simpler view.',
  loadError: "Couldn't load activity.",
  empty: 'Nothing recorded yet. The link box reports here once it is connected.',
  emptyProblems: 'No problems in this period.',
  emptyFiltered: 'Nothing matches these filters.',
  clearFilters: 'Clear filters',
  end: "That's everything kept (14 days).",
  kept: 'Activity is kept for 14 days.',
  loadOlder: 'Load older activity',
  refresh: 'Refresh activity',
  technicalLog: 'Technical log',
  plainView: 'Plain view',
  problemsOnly: 'Problems only',
  repeats: (count: number) => `This keeps happening: ${count} times in 30 min.`,
  fixedAt: (time: string) => `Fixed at ${time}`,
  routineRun: (count: number) => `${countText(count, 'routine update', 'routine updates')}`,
  connectionTitle: 'Connection to the machine',
  connectionSubtitle: 'Recent drops and reconnects',
  connectionEmpty: 'No connection changes recorded yet.',
} as const

/** Summary line above the list. */
export function activitySummary(problems: number, routine: number, fixedThemselves = 0): string {
  if (problems === 0) return `No problems today · ${countText(routine, 'routine update', 'routine updates')}`
  if (fixedThemselves > 0) return `${countText(problems, 'problem needs', 'problems need')} attention · ${new Intl.NumberFormat('en-PK').format(fixedThemselves)} fixed themselves`
  return `${countText(problems, 'problem', 'problems')} · ${countText(routine, 'routine update', 'routine updates')} today`
}

// ---------------------------------------------------------------- Activity tab (DEV-11)

/** Display kinds of the /activity items (04-device §3 Row anatomy). */
export type ActivityDisplayKind = 'problem' | 'review' | 'retried' | 'good' | 'routine' | 'action'

export const ACTIVITY_KIND_LABELS: Record<ActivityDisplayKind, string> = {
  problem: 'Problem',
  review: 'Needs checking',
  retried: 'Trying again',
  good: 'Good',
  routine: 'Routine',
  action: 'Done from the dashboard',
}
export const SERIOUS_PROBLEM = 'Serious problem'

/** Problem event labels (the flag rows). */
export const PROBLEM_EVENT_LABELS: Record<string, string> = {
  opened: 'Problem opened',
  seen: 'Marked as seen',
  fixed: 'Marked as fixed',
  cleared: 'Fixed by itself',
}

/** The API's `topic` values (be/activity_words.py TOPICS) and their chip words (Appendix B Group). */
export const ACTIVITY_TOPICS = ['machine', 'link_box', 'storage', 'hr_system', 'people', 'software'] as const
export type ActivityTopic = typeof ACTIVITY_TOPICS[number]
export const TOPIC_LABELS: Record<ActivityTopic, string> = {
  machine: 'Machine',
  link_box: 'Link box',
  storage: 'Link box memory',
  hr_system: 'HR system',
  people: 'People and CNIC',
  software: 'Software update',
}
export const topicLabel = (topic: string | null | undefined) =>
  TOPIC_LABELS[(topic ?? '') as ActivityTopic] ?? (topic === 'memory' ? TOPIC_LABELS.storage : TOPIC_LABELS.link_box)

/** Fallback rows (client dictionary): its group → the API topic. */
export const groupTopic = (group: ActivityGroup): ActivityTopic => (group === 'memory' ? 'storage' : group)

/** Display kind of a raw log row in the simpler (fallback) view. */
export function fallbackKind(described: Pick<DescribedLog, 'kind' | 'act'>, level: string | null | undefined): ActivityDisplayKind {
  if (described.kind === 'problem') return 'problem'
  if (described.act === 'list') return 'review'
  const upper = (level ?? '').toUpperCase()
  if (described.act === 'if_repeats' || upper === 'WARN' || upper === 'ERROR') return 'retried'
  return 'routine'
}

export const ACTIVITY_WORDS_TAB = {
  all: 'All',
  problemsOnly: 'Problems only',
  topicsLabel: 'Show',
  dayLabel: 'Day',
  today: 'Today',
  yesterday: 'Yesterday',
  last7: 'Last 7 days',
  pickDay: 'Pick a day…',
  live: 'Live',
  reconnecting: 'Reconnecting…',
  offline: 'Offline · showing last saved data',
  updated: (ago: string) => `Updated ${ago}`,
  newRows: (count: number) => `${count} new · Show`,
  show: 'Show',
  hide: 'Hide',
  showAll: (count: number) => `Show all ${count}`,
  loading: 'Loading…',
  copyDetails: 'Copy details',
  copied: 'Copied',
  whatToDo: 'What to do:',
  when: 'When:',
  received: 'received',
  emptyProblems: (period: string) => `No problems ${period}.`,
  emptyKind: (period: string) => `No events of this kind ${period}.`,
  periodToday: 'today',
  periodDay: 'on this day',
  periodWeek: 'in the last 7 days',
  summaryNone: (period: string, routine: string) => `Nothing needs a look ${period}. ${routine} routine checks.`,
  summarySome: (period: string, problems: string, retried: string) => `${period}: ${problems}. ${retried} retried automatically.`,
  summaryPeriodToday: 'Today',
  summaryPeriodDay: 'That day',
  summaryPeriodWeek: 'Last 7 days',
  fixedAt: (time: string) => `Fixed at ${time}`,
  levelLabel: 'Level',
  levelAny: 'Any',
  levelWarn: 'Warnings and worse',
  levelError: 'Errors and worse',
  areaLabel: 'Area',
  areaAny: 'Any',
  // Event sheet
  sheetCode: 'Code',
  sheetLevel: 'Level',
  sheetArea: 'Area',
  sheetLinkBoxTime: 'Link box time',
  sheetReceived: 'Received',
  sheetRestart: 'Restart id',
  sheetNumber: 'Number',
  sheetMessage: 'Message',
  sheetContext: 'Context',
  sheetFirst: 'First',
  sheetLast: 'Last',
  sheetCount: 'Times',
  sheetActor: 'By',
  sheetStatus: 'Status',
} as const
