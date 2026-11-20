// Device health words (02-words WORDS-5 frontend: Appendix A2, A3 and the
// device health list lines of the Words table). DLV-6 also moves App.tsx's
// health helpers into this file; these are their plain-word versions.

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
