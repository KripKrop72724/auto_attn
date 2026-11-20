// Machine words (02-words WORDS-4, Appendices A4-A8): the machine connection
// chip, its reason sentence, connection history rows, and the "what it is
// doing now" line. The rule that matters most: an offline link box never
// shows a stored activity ("Live capture" while offline 19 h, live).

import type { ConnectionEvent, Device, TerminalLink } from '../types'
import { plural } from './glossary'
import { plainStatus } from './states'
import { clockText, sinceText } from './time'
import type { Tone } from './types'

// A5. Machine connection reason (terminal_link.reason); {duration} from link.since.
export const MACHINE_LINK_REASONS: Record<string, string> = {
  LINK_UP: 'The machine connection is working.',
  STABILIZING: 'The connection is back and being watched for a few minutes.',
  STABILIZING_STALLED: 'The machine came back {duration} ago but the connection is still not steady.',
  MAINTENANCE_OVERRAN: 'A planned machine restart has gone on for {duration}.',
  ESP_DISCONNECTED: "The link box is offline, so the dashboard can't see the machine.",
  NO_TERMINAL: 'No attendance machine has been linked to this device yet.',
  HIK_LIVE_CAPTURE: "New check-ins arrive live, but the machine's older records need a check.",
  HIK_WAITING: "The link box has just started and hasn't read the machine yet.",
  HIK_NETWORK: "The machine isn't answering on the network.",
  HIK_CONFIGURATION: "The machine's address or settings in the link box are missing or wrong.",
  HIK_AUTH: 'The machine refused the user name or password saved for it.',
  HIK_HTTP_STATUS: 'The machine answered with an error.',
  HIK_OVERSIZED: 'The machine sent more data than the link box can handle.',
  HIK_SOURCE_CHANGED: "The machine's saved records changed, for example after a reset.",
  HIK_INVALID_RESPONSE: "The machine sent an answer the link box couldn't understand.",
  HIK_STORAGE: "The link box couldn't save the check-ins it read from the machine.",
  HIK_POLL_ERROR: 'Reading the machine keeps failing.',
  FLAPPING: 'The connection keeps dropping, so the link box is waiting between tries.',
}
export const MACHINE_LINK_REASON_FALLBACK = 'The link box reported a connection state the dashboard has no words for yet.'

// A6. Connection history: the `transition_reason` texts the link box writes today.
export const CONNECTION_REASONS: Record<string, string> = {
  'ESP32 firmware boot': 'The link box started.',
  'searching for authenticated ZKT terminal': 'Searching the network for the machine.',
  'opening live session to last authenticated terminal': 'Connecting to the machine it used last time.',
  'authenticated discovery probe': 'Found the machine on the network.',
  'live session authenticated and identified': 'Connected and confirmed it is the right machine.',
  'stable authenticated session': 'The connection has been steady.',
  'planned attendance truth session refresh completed': 'Planned reconnect finished.',
  'Refreshing the authenticated ZKT session after a bounded attendance truth cycle': 'Reconnecting on purpose after a record check.',
  'ZKT protocol restart accepted': 'The machine accepted a restart.',
  'ZKT session ended before the stability window': 'The connection dropped soon after connecting.',
  'established ZKT session disconnected': 'The connection to the machine dropped.',
  'authenticated discovery did not find the assigned ZKT': "Couldn't find the machine on the network.",
  'connector heartbeat': 'Status report.',
}

// A7. Doing-now line (Device.current_activity).
export const ACTIVITY_WORDS: Record<string, string> = {
  LIVE_CAPTURE: 'Recording check-ins',
  ONLINE: 'Working normally',
  LIGHT_RECONCILE: 'Quick record check',
  FULL_RECONCILE: 'Reading all records from the machine',
  HISTORICAL_RECONCILE: 'Checking older records',
  HIKVISION_POLL_2S: 'Checking the machine every 2 seconds',
  PROCESSING_COMMAND: 'Carrying out a request',
  APPLYING_CONFIG: 'Applying new settings',
  OTA_RESTART: 'Restarting to finish an update',
  STATE_LOCK_BUSY: 'Busy, status will update shortly',
  BOOTING: 'Starting up',
  DISCOVERING: 'Looking for the machine',
  CONNECTING: 'Connecting to the machine',
  RECOVERING: 'Reconnected, checking it stays steady',
  SUSPECT: 'Lost the machine, trying again',
  RETRY_WAIT: "Can't reach the machine, will try again",
  FLAPPING: 'Connection keeps dropping',
  SESSION_REFRESH: 'Reconnecting (routine)',
  RESTARTING: 'Machine restarting',
  // Older link box software
  RECONCILING: 'Checking records',
  ADD_SOURCE_RECONCILE: 'Checking records with the dashboard',
  CURRENT_TAIL_AUDIT: 'Checking the newest records',
  PRESERVING_SOURCE_BOUNDARY: 'Saving its place in the records',
  VERIFYING_IDENTITY: 'Checking the list of people',
  VERIFYING_ADMIN_LEASE: 'Checking temporary admin access',
  ENFORCING_CREDENTIAL_POLICY: 'Removing PIN or card check-ins',
  SYNCING_TIME: "Setting the machine's clock",
  SAMPLING_TIME: "Reading the machine's clock",
  SCHEDULED_RESTART: 'Restarting the machine (planned)',
}
export const ACTIVITY_NULL = 'Waiting'
export const ACTIVITY_FALLBACK = 'Working'

// A8. Hikvision machine check (device.hikvision.poll_error).
export const HIK_POLL_WORDS: Record<number, string> = {
  1: 'Machine settings incomplete',
  2: 'Machine not answering',
  3: 'Machine refused the password',
  4: 'Machine replied with an error',
  5: 'Machine reply too large',
  6: 'Machine records changed',
  7: "Machine reply didn't make sense",
  8: "Couldn't save check-ins in the link box",
}
export const HIK_POLL_FALLBACK = 'Machine check failing'
export const HIK_WAITING_FIRST_CHECK = 'Online · waiting for the first machine check'
export const HIK_RECORDING = 'Recording check-ins'

const toDate = (value: Date | number | undefined) => (value instanceof Date ? value : new Date(value ?? Date.now()))

const fill = (sentence: string, duration: string) =>
  sentence.replace('{duration}', duration || 'a while')

/** The A5 sentence for a machine connection reason. Raw states use the A6 sentence. */
export function machineLinkReasonSentence(reason: string | null | undefined, since?: string | null, now?: Date | number): string {
  if (!reason) return MACHINE_LINK_REASON_FALLBACK
  const duration = since ? sinceText(since, toDate(now)) : ''
  const known = MACHINE_LINK_REASONS[reason]
  if (known) return fill(known, duration)
  const raw = plainStatus('machineRaw', reason)
  if (!raw.unknown) return raw.explain
  return MACHINE_LINK_REASON_FALLBACK
}

export interface PlainMachineLink {
  code: string
  label: string
  tone: Tone
  /** The reason sentence (A5), for the popover. */
  sentence: string
  /** "for 12 min", computed from link.since; empty when unknown. */
  duration: string
  /** The backend sentence and codes, for Technical details only. */
  technical: { state: string; reason: string | null; raw_state: string | null; message: string | null }
}

/** Machine chip: label + tone (A4), reason sentence (A5), duration from `since`. */
export function plainMachineLink(link: TerminalLink | null | undefined, now?: Date | number): PlainMachineLink {
  const state = link?.state || 'UNKNOWN'
  const word = plainStatus('machineLink', state)
  const status = word.unknown ? plainStatus('machineLink', 'UNKNOWN') : word
  const since = link?.since ?? null
  const duration = since ? `for ${sinceText(since, toDate(now))}` : ''
  return {
    code: state,
    label: status.label,
    tone: status.tone,
    sentence: link ? machineLinkReasonSentence(link.reason, since, now) : status.explain,
    duration,
    technical: { state, reason: link?.reason ?? null, raw_state: link?.raw_state ?? null, message: link?.message ?? null },
  }
}

export interface PlainConnectionRow {
  /** "Lost the machine, trying again" (A6 state sentence); first row "First record: Connected". */
  state: string
  tone: Tone
  /** A6 reason sentence, or '' when the text has no words (it goes to Technical details). */
  reason: string
  /** " · 3 failed tries in a row" / " · dropped 2 times in 15 min", only when above 0. */
  counts: string[]
  first: boolean
  /** One line: "{state} · {reason} · {counts}". */
  sentence: string
}

/** One connection history row ("2:05 pm · Lost the machine, trying again · The connection to the machine dropped."). */
export function plainConnectionRow(row: Pick<ConnectionEvent, 'from_state' | 'to_state' | 'reason' | 'consecutive_failures' | 'flap_count_15m'>): PlainConnectionRow {
  const to = plainStatus('machineRaw', row.to_state)
  const stateText = to.unknown ? 'Connection changed' : to.explain.replace(/\.$/, '')
  const first = !row.from_state || row.from_state.toUpperCase() === 'UNKNOWN'
  const state = first ? `First record: ${stateText}` : stateText
  const reason = row.reason ? CONNECTION_REASONS[row.reason] ?? '' : CONNECTION_REASONS['connector heartbeat']
  const counts: string[] = []
  if (row.consecutive_failures > 0) counts.push(`${plural(row.consecutive_failures, 'failed try', 'failed tries')} in a row`)
  if (row.flap_count_15m > 0) counts.push(`dropped ${plural(row.flap_count_15m, 'time')} in 15 min`)
  const sentence = [state, reason.replace(/\.$/, ''), ...counts].filter(Boolean).join(' · ')
  return { state, tone: to.unknown ? 'neutral' : to.tone, reason, counts, first, sentence }
}

/** "Offline since 10:42 am · 19 h". */
export function offlineSince(since: string | null | undefined, now?: Date | number): string {
  if (!since) return 'Offline'
  return `Offline since ${clockText(since)} · ${sinceText(since, toDate(now))}`
}

/**
 * The doing-now line (A7, A8). When the link box is not connected it always
 * says "Offline since …", whatever the stored value. Hikvision devices read
 * the poll error, never `last_error_message` (that goes to Technical details).
 */
export function plainActivity(
  device: Pick<Device, 'connected' | 'current_activity' | 'firmware_family' | 'hikvision' | 'last_seen_at'>,
  now?: Date | number,
): string {
  if (!device.connected) return offlineSince(device.last_seen_at, now)
  if (device.firmware_family === 'hikvision') {
    const error = device.hikvision?.poll_error
    if (error) return HIK_POLL_WORDS[error] ?? HIK_POLL_FALLBACK
    if (!device.hikvision?.last_successful_poll_epoch) return HIK_WAITING_FIRST_CHECK
    return HIK_RECORDING
  }
  const value = device.current_activity
  if (!value) return ACTIVITY_NULL
  return ACTIVITY_WORDS[value.trim().toUpperCase()] ?? ACTIVITY_FALLBACK
}
