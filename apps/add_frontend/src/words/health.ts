// Device health words (02-words WORDS-5 frontend: Appendix A2, A3 and the
// device health list lines of the Words table), plus the health and alert
// helpers DLV-6 moved here from App.tsx (terminalLinkLabel, healthReasonLine,
// resolutionSummary, ...). The moved helpers keep today's sentences so the
// existing pages stay unchanged; page builders switch callers to the plain
// versions above them (plainTier, plainReasonLine, healthDetailWords, ...).

import { humanizeStatus, normalizedStatus, statusPattern, type StatusPattern } from '../status'
import { elapsedSince } from '../lib/time'
import type { DeviceHealth } from '../types'
import { problemWords, TEST_UPDATES_WAIT } from './problems'
import { plainStatus } from './states'
import { dayTimeText } from './time'
import type { PlainStatus } from './types'

/** A2 tier pill: DEGRADED "Needs attention", WARNING "Note", none "For information". */
export function plainTier(tier: string | null | undefined): PlainStatus {
  if (!tier || tier === 'RULE') return plainStatus('tier', 'NONE')
  const word = plainStatus('tier', tier)
  return word.unknown ? plainStatus('tier', 'NONE') : word
}

/** A3 words for the Technical details of one health reason. */
export function healthDetailWords(reason: {
  currency?: string | null
  binding?: string | null
  latch?: { kind?: string | null } | null
  firmware_version?: string | null
}): string[] {
  const lines: string[] = []
  if (reason.currency) {
    const word = plainStatus('currency', reason.currency)
    if (!word.unknown) {
      const version = reason.currency === 'PREVIOUS_BOOT' && reason.firmware_version ? ` (software ${reason.firmware_version.replace(/^zone-lite-/i, '')})` : ''
      lines.push(word.explain.replace(/\.$/, version + '.'))
    }
  }
  if (reason.binding) {
    const word = plainStatus('binding', reason.binding)
    if (!word.unknown) lines.push(word.explain)
  }
  if (reason.latch?.kind) {
    const word = plainStatus('latch', reason.latch.kind)
    if (!word.unknown) lines.push(word.explain)
  }
  return lines
}

/** "Why this device is …" heading of the device health list. */
export const WHY_TITLES: Record<string, string> = {
  ONLINE_WITH_WARNINGS: 'Why this device is working with a note',
  DEGRADED: 'Why this device needs attention',
  FLAPPING: 'Why this device needs attention',
  QUARANTINED_DUPLICATE_SERIAL: 'Why this device is blocked',
  ONBOARDING: 'Why this device is being set up',
  OFFLINE: 'Why this device is offline',
}
export const whyTitle = (state: string | null | undefined) => WHY_TITLES[(state ?? '').toUpperCase()] ?? 'About this device'

export const OFFLINE_RULE_SENTENCE =
  'A device shows as offline when the dashboard hears nothing from it for 45 seconds. It comes back as soon as it reports again.'

export { TEST_UPDATES_WAIT }

export interface ReasonLineSource {
  code?: string | null
  plain_title?: string | null
  since?: string | null
}

/** Devices list reason line: "{plain_title} · since {day time}". Never the backend message. */
export function plainReasonLine(primary: ReasonLineSource | null | undefined, now: Date = new Date()): string {
  if (!primary) return ''
  const title = problemWords(primary).title
  const since = primary.since ? dayTimeText(primary.since, now) : ''
  return since ? `${title} · since ${since}` : title
}

/** "Last problem the link box reported: {title}" (stored device error). */
export function deviceErrorLine(code: string | null | undefined): string {
  if (!code) return ''
  return `Last problem the link box reported: ${problemWords({ code }).title}`
}

/** "{title}. See Activity for what happened." (stored device error in the health list). */
export function storedErrorSentence(code: string | null | undefined): string {
  if (!code) return ''
  return `${problemWords({ code }).title}. See Activity for what happened.`
}

// ------------------------------------------------ moved from App.tsx (DLV-6)


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
