// Device health and alert words (moved from App.tsx). The Words chapter
// replaces these sentences with the plain dictionaries; callers keep the names.
import { humanizeStatus, normalizedStatus, statusPattern, type StatusPattern } from '../status'
import { elapsedSince } from '../lib/time'
import type { DeviceHealth } from '../types'

const terminalLinkLabels: Record<string, string> = {
  CONNECTED: 'Terminal connected',
  STABILIZING: 'Terminal stabilizing',
  MAINTENANCE: 'Terminal maintenance',
  STARTING: 'Terminal starting',
  RECONNECTING: 'Terminal reconnecting',
  DISCONNECTED: 'Terminal disconnected',
  FLAPPING: 'Terminal flapping',
  ERROR: 'Terminal error',
  UNKNOWN: 'Terminal unknown',
}

export const terminalLinkLabel = (state: unknown) => {
  const status = normalizedStatus(state).toUpperCase()
  return terminalLinkLabels[status] || `Terminal ${humanizeStatus(status).toLowerCase()}`
}

export const healthTierPattern = (tier: unknown): StatusPattern => {
  const value = normalizedStatus(tier).toUpperCase()
  return value === 'DEGRADED' ? 'waiting' : value === 'WARNING' ? 'notice' : statusPattern(value)
}

const tierWords: Record<string, string> = { DEGRADED: 'Degraded', WARNING: 'Warning' }

// The list carries a compact primary reason; the detail response carries the
// ordered reasons, so fall back to the first one with the worst tier.
export function primaryHealthReason(health?: DeviceHealth | null) {
  if (!health) return null
  if (health.primary) return health.primary
  const reasons = health.reasons || []
  return reasons.find((reason) => normalizedStatus(reason.tier).toUpperCase() === 'DEGRADED')
    || reasons.find((reason) => normalizedStatus(reason.tier).toUpperCase() === 'WARNING')
    || null
}

export function healthReasonLine(
  reason: { code: string; tier: string | null; message: string; since: string | null },
  now = Date.now(),
) {
  const tier = normalizedStatus(reason.tier).toUpperCase()
  const prefix = tierWords[tier] || (reason.tier ? humanizeStatus(reason.tier) : '')
  const elapsed = elapsedSince(reason.since, now)
  return `${prefix ? `${prefix}: ` : ''}${reason.message || humanizeStatus(reason.code)}${elapsed ? ` · ${elapsed}` : ''}`
}

export function formatAlertDiagnostics(details: Record<string, unknown>): string {
  const facts: string[] = []
  const category = details.failure_category
  if (typeof category === 'string' && /^[A-Z0-9_]{1,80}$/.test(category)) {
    facts.push(`Category ${category}`)
  }
  const httpStatus = details.http_status
  if (typeof httpStatus === 'number' && Number.isInteger(httpStatus) && httpStatus >= 100 && httpStatus <= 599) {
    facts.push(`HTTP ${httpStatus}`)
  }
  const attemptCount = details.attempt_count
  if (typeof attemptCount === 'number' && Number.isInteger(attemptCount) && attemptCount >= 0) {
    facts.push(`Attempt ${attemptCount}`)
  }
  const affectedUsers = details.affected_users
  if (typeof affectedUsers === 'number' && Number.isInteger(affectedUsers) && affectedUsers >= 0) {
    facts.push(`${affectedUsers} affected users`)
  }
  const evidence = record(details.evidence)
  if (typeof evidence?.summary === 'string' && evidence.summary.trim()) {
    facts.push(`Evidence: ${evidence.summary.trim().slice(0, 300)}`)
  }
  const firmware = token(details.firmware_version, /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/)
  const boot = token(details.boot_id, /^[A-Za-z0-9-]{1,64}$/)
  if (firmware || boot) facts.push([firmware && `Firmware ${firmware}`, boot && `boot ${boot.slice(0, 8)}`].filter(Boolean).join(' · '))
  const binding = token(details.binding, /^[A-Z_]{1,40}$/)
  if (binding) facts.push(bindingLabels[binding] || `Binding ${binding}`)
  const latch = token(record(details.latch)?.kind, /^[A-Z_]{1,60}$/)
  if (latch) facts.push(latchLabels[latch] || `Latch ${latch}`)
  // Rejection alerts keep one entry per message type; legacy rows carry one type.
  const types = record(details.types)
  const legacyRejection = typeof details.error_category === 'string' || typeof details.error_type === 'string'
  const candidates: unknown[] = types ? Object.keys(types) : legacyRejection ? [details.message_type] : []
  const messageTypes = candidates
    .filter((value): value is string => typeof value === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(value))
    .slice(0, 16)
  if (messageTypes.length) facts.push(`Rejected ${messageTypes.join(', ')}`)
  const resolution = record(details.resolution)
  const kind = token(resolution?.kind, /^[A-Z_]{1,40}$/)
  if (kind) facts.push(resolutionSummary(kind, token(resolution?.actor, /^[\w.@-]{1,80}$/)))
  return facts.join(' · ')
}

const record = (value: unknown) =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
const token = (value: unknown, pattern: RegExp) => typeof value === 'string' && pattern.test(value) ? value : null
const bindingLabels: Record<string, string> = {
  OBSERVED: 'Observed on its boot',
  INFERRED_CURRENT: 'Inferred on the current boot',
  INFERRED_PREVIOUS: 'Inferred on an earlier boot',
}
const latchLabels: Record<string, string> = {
  LED_LATCH_NO_IO_ERRORS: 'LED latch: no I/O errors this boot',
  LED_LATCH_STORAGE_VERIFIED: 'LED latch: storage verified this boot',
}
const resolutionLabels: Record<string, string> = {
  VERIFIED: 'Resolved on verified evidence',
  CONDITION_CLEARED: 'Resolved: condition cleared',
  MESSAGE_ACCEPTED: 'Resolved: message accepted',
  DEPLOYMENT_SUCCEEDED: 'Resolved: later deployment succeeded',
  BOOT_ENDED: 'Resolved: its boot ended',
  OPERATOR: 'Resolved by an operator',
  CLEANUP: 'Resolved by stranded-alert cleanup',
}

// Resolution kinds come from alert details.resolution.kind.
export function resolutionSummary(kind: string, actor?: string | null) {
  if (kind === 'OPERATOR' && actor) return `Resolved by ${actor}`
  const label = resolutionLabels[kind] || `Resolved: ${humanizeStatus(kind).toLowerCase()}`
  return actor ? `${label} (${actor})` : label
}
