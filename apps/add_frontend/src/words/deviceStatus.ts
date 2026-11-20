// One device-level status (03-home-devices HOME-7), used by Home, Devices, the
// device header and the People picker.
//
// Owner decision D-1: a device whose machine is not responding (or that has no
// machine linked, or whose machine reports an error) counts as **Offline**,
// exactly like the public /status page (be/public_status.py observed_status /
// offline_reason). The reason words below are copied from public_status.py
// REASONS so both pages read the same. When the backend sends the device's
// `public_status` (HOME-2), that answer wins, so the grace periods match too.

import type { Device, TerminalLink } from '../types'
import { PROBLEM_INFO } from './problems'
import { STATE_WORDS } from './states'
import type { Tone } from './types'

/** Copied word for word from be/public_status.py REASONS (keys = its reason_code). */
export const OFFLINE_REASONS: Record<string, { heading: string; sentence: string }> = {
  no_internet: { heading: 'No internet', sentence: 'The device may be switched off, or the internet at this place is not working.' },
  machine_not_responding: { heading: 'Machine not responding', sentence: 'The attendance machine may be switched off or its cable unplugged.' },
  machine_records_changed: { heading: 'Machine records changed', sentence: "The attendance machine's saved records have changed, for example after a reset, so it needs to be checked." },
  machine_settings_wrong: { heading: 'Wrong machine settings', sentence: "The attendance machine's password or settings do not match, so its attendance cannot be read." },
  machine_problem: { heading: 'Machine problem', sentence: 'The attendance machine is not giving its attendance properly, so it needs to be checked.' },
  not_set_up: { heading: 'Not set up yet', sentence: 'No attendance machine has been linked to this device yet.' },
}

/** be/public_status.py TERMINAL_ERROR_REASONS. */
const TERMINAL_ERROR_REASONS: Record<string, string> = {
  HIK_SOURCE_CHANGED: 'machine_records_changed',
  HIK_AUTH: 'machine_settings_wrong',
  HIK_CONFIGURATION: 'machine_settings_wrong',
}
/** be/public_status.py TERMINAL_DOWN. */
const TERMINAL_DOWN = new Set(['DISCONNECTED', 'ERROR'])

export type DeviceStatusGroup = 'spare' | 'offline' | 'attention' | 'setup' | 'working'

/** The /status answer for one device, when the backend sends it (HOME-2). */
export interface PublicStatusField {
  status: 'online' | 'offline' | string
  since?: string | null
  reason_code?: string | null
}

export type DeviceStatusInput = Pick<Device, 'state' | 'connected' | 'last_seen_at'> &
  Partial<Pick<Device, 'is_spare' | 'terminal_link' | 'health'>> & {
    public_status?: PublicStatusField | null
  }

export interface DeviceStatus {
  group: DeviceStatusGroup
  /** Chip label: "Working", "Needs attention", "Offline", "Setting up", "Spare (not in use)", "Blocked: duplicate device". */
  chip: string
  tone: Tone
  /** Bold reason heading ("Machine not responding"); empty for a plain Working device. */
  heading: string
  /** One sentence under the heading. */
  sentence: string
  /** When this status began, when known (ISO string). */
  since: string | null
  /** Grey "Note: {heading}." for Working devices with a note. */
  note: string | null
  /** Sort key for "Needs attention first" (lower first). */
  rank: number
  /** The code behind the heading, for data-status-code / Technical details. */
  code: string
}

const RANK: Record<DeviceStatusGroup, number> = { offline: 0, attention: 2, setup: 3, working: 4, spare: 5 }

const linkOf = (row: DeviceStatusInput): TerminalLink | null => row.terminal_link ?? row.health?.terminal_link ?? null

/** be/public_status.py offline_reason, given that the device is Offline. */
export function offlineReasonCode(row: DeviceStatusInput): string {
  const link = linkOf(row)
  if (row.health?.tier === 'OFFLINE' || row.state === 'OFFLINE' || !row.connected) return 'no_internet'
  if (link?.reason === 'NO_TERMINAL') return 'not_set_up'
  if (link?.state === 'ERROR') return TERMINAL_ERROR_REASONS[link.reason ?? ''] ?? 'machine_problem'
  return 'machine_not_responding'
}

/** True when /status would call this device Offline right now (before its grace period). */
export function isOfflineLikeStatus(row: DeviceStatusInput): boolean {
  if (row.health?.tier === 'OFFLINE' || row.state === 'OFFLINE' || !row.connected) return true
  const link = linkOf(row)
  return Boolean(link && (TERMINAL_DOWN.has(link.state) || link.reason === 'NO_TERMINAL'))
}

const offline = (reasonCode: string, since: string | null): DeviceStatus => {
  const reason = OFFLINE_REASONS[reasonCode] ?? OFFLINE_REASONS.no_internet
  return {
    group: 'offline',
    chip: STATE_WORDS.device.OFFLINE.label,
    tone: 'problem',
    heading: reason.heading,
    sentence: reason.sentence,
    since,
    note: null,
    rank: RANK.offline,
    code: reasonCode,
  }
}

const reasonHeading = (code: string | null | undefined) => {
  const info = code ? PROBLEM_INFO[code] : undefined
  return info
    ? { heading: info.title, sentence: info.detail }
    : { heading: 'Needs attention', sentence: 'Open the device for details.' }
}

interface PrimaryWithPlain {
  code: string
  since: string | null
  plain_title?: string | null
  plain_detail?: string | null
}

/**
 * The one plain status of a device. Order: Spare · Blocked: duplicate device ·
 * Offline (no internet, or the machine not responding / not linked / in error,
 * per D-1) · Setting up · Needs attention · Working (+ note).
 */
export function deviceStatus(row: DeviceStatusInput): DeviceStatus {
  const link = linkOf(row)
  const primary = (row.health?.primary ?? null) as PrimaryWithPlain | null
  if (row.is_spare) {
    return { group: 'spare', chip: STATE_WORDS.device.SPARE.label, tone: 'neutral', heading: '', sentence: 'Not counted in any totals.', since: null, note: null, rank: RANK.spare, code: 'SPARE' }
  }
  if (row.state === 'QUARANTINED_DUPLICATE_SERIAL') {
    return {
      group: 'attention',
      chip: STATE_WORDS.device.QUARANTINED_DUPLICATE_SERIAL.label,
      tone: 'problem',
      heading: 'Two link boxes claim the same machine',
      sentence: 'Two devices say they are linked to the same attendance machine. One of them must be corrected.',
      since: primary?.since ?? null,
      note: null,
      rank: 1,
      code: 'QUARANTINED_DUPLICATE_SERIAL',
    }
  }
  const pub = row.public_status
  if (pub && pub.status === 'offline') {
    return offline(pub.reason_code && OFFLINE_REASONS[pub.reason_code] ? pub.reason_code : offlineReasonCode(row), pub.since ?? null)
  }
  if (row.health?.tier === 'OFFLINE' || row.state === 'OFFLINE' || !row.connected) {
    return offline('no_internet', row.last_seen_at)
  }
  if (row.state === 'ONBOARDING') {
    return { group: 'setup', chip: STATE_WORDS.device.ONBOARDING.label, tone: 'neutral', heading: 'Being set up', sentence: 'It will show as working once its machine is linked.', since: null, note: null, rank: RANK.setup, code: 'ONBOARDING' }
  }
  if (link && (TERMINAL_DOWN.has(link.state) || link.reason === 'NO_TERMINAL')) {
    if (!pub) return offline(offlineReasonCode(row), link.since)
    // /status has not counted it yet (grace period): show it as needing attention meanwhile.
    const reason = OFFLINE_REASONS[offlineReasonCode(row)]
    return { group: 'attention', chip: STATE_WORDS.device.DEGRADED.label, tone: 'warning', heading: reason.heading, sentence: reason.sentence, since: link.since, note: null, rank: RANK.attention, code: link.state }
  }
  if (link?.state === 'FLAPPING' || row.state === 'FLAPPING') {
    return { group: 'attention', chip: STATE_WORDS.device.FLAPPING.label, tone: 'warning', heading: 'Machine connection keeps dropping', sentence: "Check the machine's network cable.", since: link?.since ?? null, note: null, rank: RANK.attention, code: 'FLAPPING' }
  }
  if (row.health?.tier === 'DEGRADED' || row.state === 'DEGRADED') {
    const words = reasonHeading(primary?.code)
    return {
      group: 'attention',
      chip: STATE_WORDS.device.DEGRADED.label,
      tone: 'warning',
      heading: primary?.plain_title?.trim() || words.heading,
      sentence: primary?.plain_detail?.trim() || words.sentence,
      since: primary?.since ?? null,
      note: null,
      rank: RANK.attention,
      code: primary?.code ?? 'DEGRADED',
    }
  }
  if (row.health?.tier === 'ONLINE_WITH_WARNINGS' || row.state === 'ONLINE_WITH_WARNINGS') {
    const heading = primary?.plain_title?.trim() || reasonHeading(primary?.code).heading
    return { group: 'working', chip: STATE_WORDS.device.ONLINE.label, tone: 'good', heading: '', sentence: '', since: null, note: `Note: ${heading}.`, rank: RANK.working, code: primary?.code ?? 'ONLINE_WITH_WARNINGS' }
  }
  return { group: 'working', chip: STATE_WORDS.device.ONLINE.label, tone: 'good', heading: '', sentence: '', since: null, note: null, rank: RANK.working, code: row.state || 'ONLINE' }
}

/** Chip words of the Devices status filter (03-home-devices Filters). */
export const DEVICE_STATUS_FILTERS: Record<Exclude<DeviceStatusGroup, 'spare'>, string> = {
  attention: 'Needs attention',
  offline: 'Offline',
  working: 'Working',
  setup: 'Setting up',
}
