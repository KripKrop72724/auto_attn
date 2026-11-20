// Attendance page words (06-attendance ATT-6): the five status chips, the one
// short note per check-in, how it arrived, the machine clock, phone messages,
// the place name and the old release lock reasons. Codes never reach the
// screen: they stay for Technical details.
import { clock, day, dayTime } from '../../lib/time'
import { deviceName } from '../../lib/deviceNames'
import type { IconName } from '../../Icon'
import type { Tone } from '../../ui/StatusChip'
import { deliveryGroup as groupOfStatus, DELIVERY_GROUPS, type DeliveryGroup } from '../../words/attendance'
import type { CheckinNotificationStatus } from '../../types'
import type { AttendanceRow } from './types'

export { plainReason } from '../../words/attendanceReasons'

export const GROUP_ORDER: readonly DeliveryGroup[] = ['sent', 'waiting', 'not_matched', 'set_aside', 'problem']

/** One status chip per check-in. Text plus icon; never the Life or Health colours. */
export const GROUP_CHIP: Record<DeliveryGroup, { label: string; tone: Tone; icon: IconName }> = {
  sent: { label: 'Sent', tone: 'working', icon: 'check' },
  waiting: { label: 'Waiting', tone: 'neutral', icon: 'clock' },
  not_matched: { label: 'Not matched', tone: 'attention', icon: 'users' },
  set_aside: { label: 'Set aside', tone: 'neutral', icon: 'pause' },
  problem: { label: 'Problem', tone: 'offline', icon: 'alert' },
}

const isGroup = (value: unknown): value is DeliveryGroup => typeof value === 'string' && (GROUP_ORDER as readonly string[]).includes(value)

/** The server's group, or the client mirror for an older backend during a deploy. */
export function rowGroup(row: Pick<AttendanceRow, 'delivery_group' | 'ords_status'>): DeliveryGroup {
  return isGroup(row.delivery_group) ? row.delivery_group : groupOfStatus(row.ords_status)
}

export const knownDeliveryCodes = () => Object.keys(DELIVERY_GROUPS)

/** "8:02 am" today, "Yesterday, 5:12 pm", "Mon 3 Nov, 8:00 am" (Pakistan time, no seconds). */
export function rowTime(value: string | null | undefined, now: Date | number = Date.now()): string {
  const text = dayTime(value, now)
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : ''
}

const secondsFormat = new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Karachi', hour: 'numeric', minute: '2-digit', second: '2-digit' })
/** "8:02:14 am" for the journey; the day is added when it is not today. */
export function journeyTime(value: string | null | undefined, now: Date | number = Date.now()): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const time = secondsFormat.format(date).replace(/\s?AM$/, ' am').replace(/\s?PM$/, ' pm')
  const short = rowTime(value, now)
  return short.includes(',') ? `${short.slice(0, short.lastIndexOf(',') + 1)} ${time}` : time
}

/** "Thu 6 Nov, 8:02 am" for the sheet header. */
export const longTime = (value: string | null | undefined, now: Date | number = Date.now()) =>
  value ? `${day(value, now)}, ${clock(value)}` : ''

/** "Check-in" / "Check-out" / "Other type (4)" / "Type not recorded". */
export function checkinKind(punch: string | number | null | undefined): string {
  if (punch === null || punch === undefined || punch === '') return 'Type not recorded'
  const value = Number(punch)
  if (value === 0) return 'Check-in'
  if (value === 1) return 'Check-out'
  return `Other type (${punch})`
}

export const displayIdentity = (row: AttendanceRow) => ({
  display_name: row.display_name || row.force_release?.approved_identity?.display_name || null,
  cnic_masked: row.cnic_masked || row.force_release?.approved_identity?.cnic_masked || null,
})

export const personName = (row: AttendanceRow) => displayIdentity(row).display_name || 'Name not known'

/** "Building 9 · Device 3" from the link box that delivered the check-in. */
export function placeName(row: AttendanceRow): string | null {
  const connector = row.source_connector
  if (!connector) return null
  if (connector.friendly_name) return connector.friendly_name
  const name = deviceName(connector.display_name || connector.zone_id)
  return name.unit ? `${name.location} · ${name.unit}` : name.location
}

export function placeDivision(row: AttendanceRow): 'life' | 'health' | null {
  const connector = row.source_connector
  if (!connector) return null
  if (connector.division === 'life' || connector.division === 'health') return connector.division
  return deviceName(connector.display_name || connector.zone_id).zone
}

/** The row's place line: never the device found by serial when the serial is missing. */
export function placeLine(row: AttendanceRow): string {
  const place = placeName(row)
  if (!place) return 'Place not known'
  return row.device_serial ? place : `Came through ${place}`
}

const WAITING_NOTES: Record<string, string> = {
  IN_FLIGHT: 'Sending now',
  FAILED_RETRYABLE: 'Trying again',
  RETRYING: 'Trying again',
  ACKED_FIRMWARE: 'Checking with the HR system',
  FIRMWARE_RECEIPT_UNVERIFIED: 'Checking with the HR system',
  FIRMWARE_RECEIPT_VERIFYING: 'Checking with the HR system',
  MEMBERSHIP_REVERIFYING: 'Checking with the HR system',
  MEMBERSHIP_REVERIFY_RETRY: 'Checking with the HR system',
  PENDING: 'Will be sent automatically',
}

export const NOTES = {
  wrongTime: 'Wrong time on the machine · not sent until the time is fixed',
  oldRecord: "Old record the HR system can't accept",
  someoneElse: "Someone else had this Employee ID then · can't be sent",
  twoShare: 'Two people share this CNIC · fix it on the People page',
  cnicChanged: 'CNIC changed · you choose which one to send',
  cnicChangedNear: 'CNIC changed around this time · you choose which one to send',
  noCnic: 'No CNIC for this person',
  machineMatchAuto: 'Not matched · CNIC found on the machine, sending by itself',
  machineMatchPress: 'Not matched · CNIC found on the machine, press Send',
  machineNotConfirmed: 'Machine not confirmed',
  refused: 'The HR system refused it',
} as const

/** True when the row has no CNIC link (the server's answer wins). */
export const noCnic = (row: AttendanceRow) =>
  row.cnic_not_linked ?? (!displayIdentity(row).cnic_masked && !row.direct_ords_identity?.cnic_source)

/**
 * The one short note under the status chip (first match wins). `autoSending`
 * is the automatic sending switch: null while it is not known.
 */
export function statusNote(row: AttendanceRow, autoSending: boolean | null = true, now: Date | number = Date.now()): string {
  const group = rowGroup(row)
  const status = (row.ords_status || '').toUpperCase()
  const identity = row.direct_ords_identity
  const exclusion = identity?.exclusion
  if (group === 'sent') {
    const at = row.oracle_confirmed_at ? rowTime(row.oracle_confirmed_at, now) : ''
    if (row.force_release) return at ? `Sent by an admin · ${at}` : 'Sent by an admin'
    return at ? `Sent ${at}` : 'Sent'
  }
  if (group === 'waiting') return WAITING_NOTES[status] || 'Will be sent automatically'
  if (group === 'problem') return status === 'QUARANTINED_ORDS_REJECTED' ? NOTES.refused : 'Needs checking · see Technical details'
  if (group === 'set_aside') {
    if (status === 'QUARANTINED_INVALID_DEVICE_TIME' || exclusion === 'CLOCK_INVALID' || exclusion === 'TIME_IMPOSSIBLE') return NOTES.wrongTime
    if (status === 'QUARANTINED_INVALID_EVENT_UID') return NOTES.oldRecord
    if (status === 'QUARANTINED_MISSING_TERMINAL_PROVENANCE') return 'Machine not confirmed · set aside'
    if (status === 'QUARANTINED_IDENTITY_CONFLICT') return 'Two people share this Employee ID · set aside'
    if (status === 'QUARANTINED_SOURCE_CONFLICT') return "The machine's records don't match · set aside"
    return 'Set aside for checking'
  }
  // Not matched. Reasons that stop sending come before the ones that allow it.
  if (!row.device_serial) return NOTES.machineNotConfirmed
  if (exclusion === 'CNIC_CONFLICT') return NOTES.someoneElse
  if (exclusion === 'IDENTITY_CONFLICT_UNRESOLVED') return NOTES.twoShare
  if (exclusion === 'CLOCK_INVALID' || exclusion === 'TIME_IMPOSSIBLE') return NOTES.wrongTime
  if (exclusion === 'INVALID_EVENT_UID') return NOTES.oldRecord
  if (identity?.punch_time_cnic === 'DIFFERS') return NOTES.cnicChanged
  if (identity?.punch_time_cnic === 'CHANGED_NEAR_PUNCH') return NOTES.cnicChangedNear
  if (!displayIdentity(row).display_name && identity?.cnic_source === 'SYNCED_USER') {
    return autoSending === false ? NOTES.machineMatchPress : NOTES.machineMatchAuto
  }
  if (noCnic(row)) return NOTES.noCnic
  if (status === 'QUARANTINED_IDENTITY_REUSE') return 'Someone else used this Employee ID then'
  return "Waiting for the person's details"
}

/** Why a check-in that is set aside or refused is never sent automatically (sheet). */
export function neverSentSentence(row: AttendanceRow): string | null {
  const group = rowGroup(row)
  if (group === 'problem') {
    return row.ords_status === 'QUARANTINED_ORDS_REJECTED'
      ? 'The HR system refused this check-in, so it is not sent again by itself. IT can see the reason under Technical details.'
      : 'This check-in has a status the dashboard does not know, so it is not sent by itself. IT can see it under Technical details.'
  }
  if (group === 'set_aside') {
    const note = statusNote(row)
    if (note === NOTES.wrongTime) return 'The machine had the wrong time, so this check-in is not sent until the time is fixed.'
    if (note === NOTES.oldRecord) return "This is an old record the HR system can't accept, so it is kept here and not sent."
    if (note.startsWith('Machine not confirmed')) return "The dashboard can't confirm which machine this came from, so it is not sent by itself."
    return 'This check-in is set aside for checking, so it is never sent automatically.'
  }
  return null
}

/** Journey line for how the check-in reached the dashboard. */
export function arrivalWords(source: string | null | undefined): string {
  const value = (source || '').toUpperCase()
  if (value === 'LIVE' || value === 'LIVE_POLL') return 'Live'
  if (value === 'DUMP_RECONNECT') return 'Collected later, after the machine came back online'
  if (value === 'DUMP_STARTUP') return 'Collected later, after the machine restarted'
  if (['FULL_HISTORY', 'CURRENT_RECONCILE', 'RECONCILE_15M'].includes(value)) return 'Collected later, read straight from the machine'
  return 'Collected later'
}

/** Journey line for the machine clock. Never shown on the row. */
export function clockWords(quality: string | null | undefined, driftSeconds?: number | null): string {
  const value = (quality || '').toUpperCase()
  if (value === 'OK') return 'Machine time correct'
  if (value === 'DRIFTED') {
    const minutes = driftSeconds == null ? null : Math.max(1, Math.round(Math.abs(driftSeconds) / 60))
    return minutes === null ? 'Machine time was off' : `Machine time was off by ${minutes} min`
  }
  if (value === 'INVALID') return 'Wrong time on the machine'
  if (value === 'OPERATOR_CORRECTED') return 'Time corrected by an admin'
  return 'Machine time not checked'
}

// Phone messages -------------------------------------------------------------

export const PHONE_STATUS_WORDS: Record<CheckinNotificationStatus, string> = {
  NOTIFIED: 'Delivered',
  PENDING: 'Waiting',
  RETRYING: 'Trying again',
  FAILED: 'Failed',
  NOT_SENT: 'Not sent',
}

/** URL value ⇄ server value for the phone message filter. */
export const PHONE_FILTERS = {
  delivered: 'NOTIFIED',
  waiting: 'PENDING',
  trying_again: 'RETRYING',
  failed: 'FAILED',
  not_sent: 'NOT_SENT',
} as const satisfies Record<string, CheckinNotificationStatus>
export type PhoneFilter = keyof typeof PHONE_FILTERS

// Old release lock reasons (Technical details › "Old release state" only) ----

const LOCK_WORDS: Record<string, string> = {
  TARGET_CNIC_MISSING: "Add the person's CNIC first",
  TARGET_NOT_ACTIVE: "The person isn't on the machine any more",
  TARGET_SNAPSHOT_UNSTABLE: "The machine's people list is still changing",
  IDENTITY_SNAPSHOT_UNSTABLE: "The machine's people list is still changing",
  TARGET_DUPLICATE_CNIC_UNRESOLVED: 'Two people share this CNIC',
  CLOCK_NOT_OK: 'Wrong time on the machine',
  QUARANTINED_INVALID_DEVICE_TIME: 'Wrong time on the machine',
  QUARANTINED_INVALID_EVENT_UID: "Old record the HR system can't accept",
  QUARANTINED_ORDS_REJECTED: 'The HR system refused it',
  SOURCE_RECERTIFICATION_REQUIRED: 'The machine needs a full check first',
  TERMINAL_RELEASE_IN_PROGRESS: 'Another sending task is running for this machine',
}

export const explainReleaseLock = (code: string | null | undefined) =>
  (code && LOCK_WORDS[code]) || "Can't be sent (see code)"

// Fix tools: why some were not sent (short, read as "N check-ins {reason}") --

export const SKIP_WORDS: Record<string, string> = {
  CNIC_CHOICE_REQUIRED: 'need you to choose the CNIC',
  CNIC_CONFLICT: 'someone else had this Employee ID',
  PUNCH_TIME_CNIC_HELD_BY_OTHER: 'the old CNIC now belongs to someone else',
  IDENTITY_CONFLICT_UNRESOLVED: 'two people share the CNIC',
  CLOCK_INVALID: 'wrong time on the machine',
  TIME_IMPOSSIBLE: 'impossible time',
  INVALID_EVENT_UID: "old record the HR system can't accept",
  ALREADY_CONFIRMED: 'already sent',
  ALREADY_DELIVERING: 'already being sent',
  UNKNOWN_USER: 'person not on the machine',
  CNIC_MISSING: 'no CNIC on the machine',
}
export const skipWords = (code: string) => SKIP_WORDS[code] || 'not sent for safety'

export const checkins = (count: number) => `${count.toLocaleString('en-PK')} ${count === 1 ? 'check-in' : 'check-ins'}`

/** Plain sentence for a failed request; never "ApiError: …". */
export function plainError(error: unknown, fallback = "Couldn't do this. Try again."): string {
  const status = typeof (error as { status?: unknown })?.status === 'number' ? (error as { status: number }).status : null
  const code = typeof (error as { code?: unknown })?.code === 'string' ? (error as { code: string }).code : null
  if (code === 'ADD_BUSY') return 'Busy · trying again…'
  if (status === 502 || status === 503 || status === 504) return "The server didn't answer. Try again in a minute."
  if (status === 401 || code === 'STEP_UP_FAILED' || code === 'INVALID_PASSWORD') return "That password isn't right."
  const plain = (error as { plainMessage?: unknown })?.plainMessage
  if (typeof plain === 'string' && plain) return plain
  return fallback
}
