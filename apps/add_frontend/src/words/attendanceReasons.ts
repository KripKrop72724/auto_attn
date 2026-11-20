// Attendance fix tool words (02-words WORDS-8, Appendix H): per-check-in
// reasons, blockers, waiting lines and release lock reasons. Stored codes and
// backend sentences stay as they are; the backend sentence goes to Technical
// details. 06-attendance's attendanceWords.ts re-exports plainReason().

export type ReasonDomain = 'force' | 'direct' | 'skip' | 'recoveryRow' | 'recoveryItem' | 'areaWait' | 'lock'

// H1. Force release (be/attendance_force_release.py MESSAGES, plus the two fixed sentences).
export const FORCE_REASONS: Record<string, string> = {
  READY: 'Same person and CNIC on the machine now. An admin must approve sending it.',
  CNIC_MISSING: 'No CNIC for this person, on the machine or in the dashboard.',
  CNIC_CONFLICT: "The CNIC doesn't match.",
  USER_MISSING: "This person is no longer on the machine, or can't be identified.",
  IDENTITY_CONFLICT: "The person's details changed or have a known conflict.",
  USER_ID_INVALID: "The saved Employee ID is damaged or doesn't match.",
  UID_INVALID: "The saved Employee ID is damaged or doesn't match.",
  UID_UNPROVEN: "This record doesn't show which person it belongs to.",
  EVENT_ID_INVALID: "The check-in's record number is damaged.",
  SOURCE_INVALID: 'The original check-in record needs checking.',
  TIME_INVALID: 'The check-in date or time needs checking.',
  TERMINAL_CHANGED: "The machine changed or isn't confirmed.",
  SNAPSHOT_CHANGED: "The machine's list of people changed. Re-read it and check again.",
  SYNC_FAILED: "Couldn't re-read the full list of people from the machine.",
  SYNC_EXPIRED: 'Re-reading the list of people took too long. Try this device again later.',
  DEVICE_OFFLINE: "The device is offline, so the list of people can't be re-read.",
  DEVICE_BUSY: 'Another change to people is using this machine. Try again when it finishes.',
  ALREADY_CONFIRMED: 'The HR system already has this check-in.',
  ALREADY_DELIVERING: "This check-in is already being sent. It won't be sent twice.",
  NOT_HELD: "This check-in isn't waiting for matching; it needs a different check.",
  PERMANENT_REVIEW: "This record has a conflict that sending anyway can't override.",
  EVIDENCE_CHANGED: "The approved person or check-in changed, so it wasn't sent.",
  STOPPED: 'Stopped before sending. The check-in stays saved.',
  ROLLOUT_RESTRICTED: "Sending anyway isn't switched on for this device yet.",
  ORACLE_UNAVAILABLE: "The HR system can't be checked right now. The approved check-in is saved.",
  ORACLE_CONFLICT: 'The HR system has different attendance or employee details. It needs review.',
  TERMINAL_NEWEST_RECORD_UNCONFIRMED: "This check-in was read only from the machine's newest record, which may not be final yet. Run Recover attendance first.",
  WAITING_ORACLE: 'Saved for sending. Waiting for the HR system to confirm it.',
  CONFIRMED: 'The HR system confirmed this check-in.',
}
/** `run.last_error` is always one sentence today (be/attendance_force_release.py). */
export const FORCE_RUN_ERROR = "The last step couldn't be saved. It will try again by itself."

// H2. Send saved check-ins to the HR system (be/attendance_direct_ords.py REASONS).
export const DIRECT_REASONS: Record<string, string> = {
  CLOCK_INVALID: "The machine's clock was wrong for this check-in. It is never sent.",
  TIME_IMPOSSIBLE: 'The check-in time is impossible (before 2010 or later than it was received). It is never sent.',
  CNIC_CHOICE_REQUIRED: "The records don't show this person's CNIC at the time of the check-in. Choose which CNIC to send.",
  CNIC_CONFLICT: 'Another CNIC or person used this Employee ID around that time. It is never sent by hand; ask HR who checked in. It stays saved.',
  IDENTITY_CONFLICT_UNRESOLVED: 'Two people on the machine have this CNIC. Sort it out in People first; the check-in stays saved.',
  PUNCH_TIME_RECORDS_TOO_MANY: 'There are too many records for this Employee ID at that time to check them all. Nothing was sent; it stays saved.',
  PUNCH_TIME_CNIC_UNREADABLE: "The CNIC saved for that time can't be read. Nothing was sent.",
  PUNCH_TIME_NAME_MISSING: "There's no employee name for that CNIC. Nothing was sent.",
  PUNCH_TIME_CNIC_DIFFERS: 'After approval, another CNIC or person showed up for this Employee ID at that time. The approval was stopped.',
  AUTO_RELEASE_PENDING: 'This check-in will be sent automatically at the next read of the list of people. Confirm only if it must go now.',
  SUPERSEDED: 'A later approval replaced this one.',
  AUTO_RELEASED: 'The dashboard matched it by itself after the approval stopped; the HR system confirmed it.',
  CNIC_CHOICE_STALE: 'The records changed after the checks loaded. Load the checks again and choose again. Nothing was sent.',
  PUNCH_TIME_CNIC_HELD_BY_OTHER: 'Another person on the machine has that CNIC today. Send it only after HR confirms who checked in.',
  PREVIOUS_POST_UNCERTAIN: 'An earlier approval may already have reached the HR system under another person. Check the HR system record first.',
  TAKEOVER_UNCERTAIN: 'An earlier approval may already have reached the HR system under another person. Check the HR system record first.',
  INVALID_EVENT_UID: "The HR system can't accept this older record number. The check-in stays saved for repair.",
  APPROVAL_UNREADABLE: "The saved approval can't be read. Nothing was sent.",
  CHANGED_BEFORE_SENDING: 'The check-in or the person changed before sending.',
  WAITING_ORACLE: 'Waiting for the HR system to confirm it.',
  CHECKING_COPY: 'Checking the copy already in the HR system.',
}

// Unknown-identity skip reasons (UnknownSyncedRelease). Read as "… skipped: {reason}".
export const SKIP_REASONS: Record<string, string> = {
  CNIC_CHOICE_REQUIRED: 'need a CNIC decision',
  CNIC_CONFLICT: 'another CNIC or person on record',
  PUNCH_TIME_CNIC_HELD_BY_OTHER: 'the CNIC belongs to another person on the machine',
  IDENTITY_CONFLICT_UNRESOLVED: 'two people share this CNIC',
  CLOCK_INVALID: 'the machine clock was wrong',
  TIME_IMPOSSIBLE: 'impossible check-in time',
  INVALID_EVENT_UID: 'older record number needs repair',
  ALREADY_CONFIRMED: 'already in the HR system',
  ALREADY_DELIVERING: 'already being sent',
  UNKNOWN_USER: 'person not found on the machine',
  CNIC_MISSING: "no CNIC on the machine's record",
}

// H4. Recovery row reasons (reason_code added by the serializer through LANE_REASON_CODES).
export const RECOVERY_ROW_REASONS: Record<string, string> = {
  CONFIRMED: 'The HR system already has this check-in.',
  MACHINE_NOT_CONFIRMED: "The dashboard can't prove which machine this came from.",
  RECORD_NUMBER_INVALID: "The check-in's record number is damaged.",
  STATE_UNKNOWN: 'Its sending status is unknown, so it is set aside for safety.',
  NEEDS_REVIEW: 'It needs a person to review it.',
  TIME_NEEDS_FIX: 'The check-in time needs to be corrected first.',
  USER_NUMBER_REUSED: 'This Employee ID belonged to someone else before, so it is set aside.',
  SEND_STATE_FINAL: 'Its sending already finished or needs review.',
}

// Recovery item error codes (AttendanceRecovery item.error_code). Unknown codes use the fallback.
export const RECOVERY_ITEM_REASONS: Record<string, string> = {
  WAITING: 'Waiting its turn',
}

// H5. Area release waiting lines (run.waiting_reason).
export const AREA_WAIT_REASONS: Record<string, string> = {
  WAITING_FOR_COMPLETE_ROSTER: 'Waiting for a complete list of people from a chosen machine before checking more check-ins.',
  WAITING_FOR_SERIAL_CONFIRMATION: "Waiting for a chosen machine's serial number to be confirmed before checking more check-ins.",
  WAITING_FOR_ROSTER_UPDATE: 'A machine sent a new list of people during the last batch. Those check-ins are checked again.',
}

// H6. Release lock reasons (Attendance.tsx and AttendanceRelease.tsx).
const all = (codes: string[], sentence: string) => Object.fromEntries(codes.map((code) => [code, sentence]))
export const LOCK_REASONS: Record<string, string> = {
  PREVIEW_DISABLED: 'Releasing check-ins is switched off for now.',
  EXECUTION_DISABLED: 'Releases are paused for now.',
  CONNECTOR_INACTIVE: "This machine isn't connected to the dashboard.",
  ...all(['NO_CONNECTOR', 'NO_TERMINAL'], "The dashboard can't find the machine for this check-in."),
  TARGET_NOT_ACTIVE: 'This employee is no longer on the machine.',
  TARGET_CNIC_MISSING: "Add the employee's CNIC first.",
  ...all(['TARGET_CNIC_INVALID', 'TARGET_CNIC_UNREADABLE'], "The saved CNIC isn't valid; correct it first."),
  TARGET_NAME_MISSING: "The employee's name is missing; add it first.",
  TARGET_NOT_IN_CURRENT_SNAPSHOT: "The employee isn't in the machine's latest list of people.",
  ...all(['TARGET_SNAPSHOT_UNSTABLE', 'IDENTITY_SNAPSHOT_UNSTABLE'], 'The list of people is still updating; try again shortly.'),
  TARGET_DUPLICATE_CNIC_UNRESOLVED: 'Two people share this CNIC; decide in People first.',
  ...all(['TARGET_IDENTITY_AMBIGUOUS', 'SOURCE_IDENTITY_AMBIGUOUS'], 'More than one employee could match; it needs a decision.'),
  ...all(['SOURCE_IDENTITY_UNPROVEN', 'REUSE_SOURCE_UNPROVEN'], "The dashboard can't prove this check-in belongs to this employee."),
  ...all(['REUSE_NAME_MISMATCH', 'SOURCE_NAME_MISMATCH'], "The name on the check-in differs from the employee's name."),
  ...all(['CLOCK_NOT_OK', 'QUARANTINED_INVALID_DEVICE_TIME'], 'The check-in time is impossible, so it stays locked.'),
  QUARANTINED_INVALID_EVENT_UID: 'An older record number must be repaired first.',
  QUARANTINED_ORDS_REJECTED: 'The HR system refused this check-in.',
  SOURCE_RECERTIFICATION_REQUIRED: 'The machine has new records; the dashboard reads them first.',
  LIVE_ORDS_BACKLOG_HIGH: 'New check-ins are still being sent; wait for them to finish.',
  ...all(['RELEASE_ALREADY_IN_PROGRESS', 'TERMINAL_RELEASE_IN_PROGRESS'], 'Another release for this machine is running.'),
  ALREADY_RELEASED: 'Already in the HR system.',
  DUPLICATE_SERIAL_QUARANTINE: 'This machine needs an admin check before it can be used.',
  SERIAL_UNKNOWN: "The dashboard hasn't confirmed this machine yet.",
  SOURCE_RECONCILIATION_DISABLED: "Reading the machine's records is switched off for now.",
  RELEASE_CANCELLED: 'The earlier release was cancelled.',
  NOT_ELIGIBLE: "This check-in can't be released safely yet.",
}

const DICTIONARIES: Record<ReasonDomain, Record<string, string>> = {
  force: FORCE_REASONS,
  direct: DIRECT_REASONS,
  skip: SKIP_REASONS,
  recoveryRow: RECOVERY_ROW_REASONS,
  recoveryItem: RECOVERY_ITEM_REASONS,
  areaWait: AREA_WAIT_REASONS,
  lock: LOCK_REASONS,
}

export const REASON_FALLBACKS: Record<ReasonDomain, string> = {
  force: 'This check-in needs a closer look before it can be sent.',
  direct: 'This check-in needs a closer look before it can be sent.',
  skip: 'skipped for safety',
  recoveryRow: 'This check-in needs a closer look before it can be sent.',
  recoveryItem: "This one couldn't be done. Details are under Technical details.",
  areaWait: 'Waiting before checking more check-ins.',
  lock: "This check-in can't be released safely yet.",
}

export interface PlainReason {
  sentence: string
  code: string
  /** The backend sentence, for Technical details only. */
  technical: string | null
  unknown: boolean
}

/**
 * The plain sentence for a reason code. `fallbackText` is the backend
 * sentence: it is never shown as the sentence, only kept for Technical details.
 */
export function plainReason(domain: ReasonDomain, code: string | null | undefined, fallbackText?: string | null): PlainReason {
  const key = (code ?? '').trim().toUpperCase()
  const sentence = key ? DICTIONARIES[domain][key] : undefined
  if (domain === 'recoveryItem' && !key) {
    return { sentence: 'Waiting its turn', code: '', technical: fallbackText ?? null, unknown: false }
  }
  return {
    sentence: sentence ?? REASON_FALLBACKS[domain],
    code: code ?? '',
    technical: fallbackText ?? null,
    unknown: !sentence,
  }
}

/** Attendance tool fixed texts from the Words table. */
export const ATTENDANCE_TOOL_WORDS = {
  impossibleTime: 'Impossible check-in time; it is never sent to the HR system.',
  twoRecords: (sentence: string) => `Two records for this person: ${sentence}`,
  fixTimesEyebrow: 'Fix check-in times',
  savedRunEyebrow: 'Saved recovery run',
  otherWay: 'Other way',
  otherReason: 'Other reason',
} as const
