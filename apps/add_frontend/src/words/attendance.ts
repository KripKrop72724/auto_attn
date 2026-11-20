// Attendance words (02-words Appendix D): the five delivery groups used by the
// Attendance status chip and filter, check-in type words, and a re-export of
// the fix-tool reasons. Only ACKED and ACKED_CHECK count as sent (fixes the
// `startsWith('ACK')` count that included ACKED_FIRMWARE).

import { plainStatus } from './states'
import type { PlainStatus, Tone } from './types'

export { plainReason, ATTENDANCE_TOOL_WORDS } from './attendanceReasons'

export type DeliveryGroup = 'sent' | 'waiting' | 'not_matched' | 'set_aside' | 'problem'

export const DELIVERY_GROUP_WORDS: Record<DeliveryGroup, { label: string; tone: Tone }> = {
  sent: { label: 'Sent', tone: 'good' },
  waiting: { label: 'Waiting', tone: 'neutral' },
  not_matched: { label: 'Not matched', tone: 'warning' },
  set_aside: { label: 'Set aside', tone: 'warning' },
  problem: { label: 'Problem', tone: 'problem' },
}

/** D1 group of each delivery code. Unknown codes are a Problem (never silently "sent"). */
export const DELIVERY_GROUPS: Record<string, DeliveryGroup> = {
  ACKED: 'sent',
  ACKED_CHECK: 'sent',
  ACKED_FIRMWARE: 'waiting',
  FIRMWARE_RECEIPT_UNVERIFIED: 'waiting',
  FIRMWARE_RECEIPT_VERIFYING: 'waiting',
  MEMBERSHIP_REVERIFYING: 'waiting',
  MEMBERSHIP_REVERIFY_RETRY: 'waiting',
  PENDING: 'waiting',
  IN_FLIGHT: 'waiting',
  FAILED_RETRYABLE: 'waiting',
  RETRYING: 'waiting',
  BLOCKED_IDENTITY: 'not_matched',
  CNIC_NOT_LINKED: 'not_matched',
  QUARANTINED_IDENTITY_REUSE: 'not_matched',
  QUARANTINED_IDENTITY_CONFLICT: 'set_aside',
  QUARANTINED_SOURCE_CONFLICT: 'set_aside',
  QUARANTINED_INVALID_DEVICE_TIME: 'set_aside',
  QUARANTINED_INVALID_EVENT_UID: 'set_aside',
  QUARANTINED_MISSING_TERMINAL_PROVENANCE: 'set_aside',
  QUARANTINED_ORDS_REJECTED: 'problem',
  MISSING_STATUS: 'problem',
}

export const deliveryGroup = (code: string | null | undefined): DeliveryGroup =>
  DELIVERY_GROUPS[(code ?? '').toUpperCase()] ?? 'problem'

/** Codes of one group, for the server's `ords_status` list filter. */
export const deliveryCodes = (group: DeliveryGroup): string[] =>
  Object.entries(DELIVERY_GROUPS).filter(([, value]) => value === group).map(([code]) => code)

/** Only these count as "Sent to HR system". */
export const isSentToHr = (code: string | null | undefined) => deliveryGroup(code) === 'sent'

export const deliveryStatus = (code: string | null | undefined): PlainStatus => plainStatus('delivery', code || 'MISSING_STATUS')

/** D3 punch type: 0 Check-in, 1 Check-out, other "Other (n)", empty "Type not given". */
export function checkinType(punch: number | string | null | undefined): string {
  if (punch === null || punch === undefined || punch === '') return 'Type not given'
  const value = Number(punch)
  if (value === 0) return 'Check-in'
  if (value === 1) return 'Check-out'
  return `Other (${punch})`
}
