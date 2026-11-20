// People words (07-people PPL-1, code-keyed part): why changes are off,
// problem chips, backend messages that reach People word for word, and
// initials. Request type and status words come from words/requests.ts.
// "Employee ID" is the machine's number (D-24); HR's number is
// "Employee ID in HR's records".

import { plural } from './glossary'
import type { Tone } from './types'

export const EMPLOYEE_ID = 'Employee ID'
export const HR_EMPLOYEE_ID = "Employee ID in HR's records"
export const HR_EMPLOYEE_ID_HELP = 'It may differ from the Employee ID on the machine.'

// Why changes are off (writes_disabled_reason, UsersWorkspace actionReason).
export const WRITES_OFF: Record<string, string> = {
  TERMINAL_SERIAL_CONFIRMATION_REQUIRED: 'Confirm this machine first. It is a one-time check.',
  TERMINAL_SERIAL_PENDING_DEVICE_ACK: 'Waiting for the link box to save your confirmation.',
  TERMINAL_SERIAL_PIN_FAILED: "The last confirmation didn't go through. Check the serial number and try again.",
  STABILITY_CERTIFICATION_PENDING: 'Machine confirmed. A short safety check is still running.',
  HIKVISION_WRITE_QUALIFICATION_PENDING: "Changing people from the dashboard isn't ready for this machine model yet. You can still see everyone. Confirming the machine again won't change this.",
  FULL_USER_SNAPSHOT_REQUIRED: 'The full list of people has to be read from the machine first.',
  USER_SNAPSHOT_TRUNCATED: 'The last read of the list was incomplete. Refresh the people list from the machine, then try again.',
  LEGACY_28_BYTE_RECORD: "This older machine model can't be changed from the dashboard.",
  SERIAL_MISMATCH: "This machine's serial number doesn't match the one confirmed before. Was the machine replaced?",
  DUPLICATE_SERIAL: 'Another device reports the same machine. Ask IT.',
  RECOVERY_SERIAL_PENDING_PROOF: 'This machine was reset or replaced. IT has to confirm it first.',
  DEVICE_NOT_WRITE_CERTIFIED: "This machine isn't approved for changes from the dashboard.",
}
export const WRITES_OFF_FALLBACK = 'Changes are off for this machine right now. Ask IT.'

/** Plain reason why adding or changing people is off on this machine. */
export function writesOff(code: string | null | undefined): { sentence: string; unknown: boolean } {
  const sentence = WRITES_OFF[(code ?? '').toUpperCase()]
  return sentence ? { sentence, unknown: false } : { sentence: WRITES_OFF_FALLBACK, unknown: true }
}

export const CAPABILITY_OFF = {
  create: "This machine doesn't allow adding people from the dashboard.",
  update: "This machine doesn't allow changing people from the dashboard.",
  delete: "This machine doesn't allow removing people from the dashboard.",
} as const

export interface PersonFacts {
  cnic_available?: boolean | null
  cnic_format_suspect?: boolean | null
  identity_conflict_code?: string | null
  identity_conflict_resolved?: boolean | null
  identity_conflict_members?: Array<string | number> | null
  current_command_state?: string | null
  privilege?: number | null
}

export interface PersonProblem {
  key: 'no_cnic' | 'cnic_typed' | 'same_cnic' | 'same_person' | 'sending' | 'admin'
  label: string
  tone: Tone | 'info'
}

const idList = (members: Array<string | number> | null | undefined) => {
  const list = (members ?? []).map(String)
  if (list.length <= 2) return list.join(' and ')
  return `${list[0]} and ${plural(list.length - 1, 'more', 'more')}`
}

/** One plain chip per fact (07-people P4). */
export function personProblems(user: PersonFacts): PersonProblem[] {
  const chips: PersonProblem[] = []
  if (user.cnic_available === false && !user.cnic_format_suspect) chips.push({ key: 'no_cnic', label: 'No CNIC', tone: 'warning' })
  if (user.cnic_format_suspect) chips.push({ key: 'cnic_typed', label: 'CNIC typed wrongly', tone: 'warning' })
  if (user.identity_conflict_code && !user.identity_conflict_resolved) {
    const others = idList(user.identity_conflict_members)
    chips.push({ key: 'same_cnic', label: others ? `Same CNIC as ${others}` : 'Same CNIC as someone else', tone: 'problem' })
  }
  if (user.identity_conflict_resolved) {
    const others = idList(user.identity_conflict_members)
    chips.push({ key: 'same_person', label: others ? `Same person as ${others} (confirmed)` : 'Same person (confirmed)', tone: 'neutral' })
  }
  if (user.current_command_state) chips.push({ key: 'sending', label: 'Sending to machine…', tone: 'info' })
  if (user.privilege === 14) chips.push({ key: 'admin', label: 'Machine admin', tone: 'neutral' })
  return chips
}

// Backend messages that reach People word for word (matched by exact text or prefix).
export const PEOPLE_ERRORS: Array<{ match: string; prefix?: boolean; sentence: string }> = [
  { match: 'No assigned ZKT device.', sentence: 'No attendance machine is linked to this device yet.' },
  { match: 'Connector not found.', sentence: 'This device no longer exists. Go back to People.' },
  { match: 'Password confirmation failed.', sentence: 'Wrong password. Try again.' },
  { match: 'This connector is quarantined because its ZKT serial is duplicated.', sentence: 'This device is blocked because another device reports the same machine. Ask IT.' },
  { match: 'A complete fresh user snapshot is required before user mutation.', sentence: 'Refresh the people list from the machine first, then try again.' },
  { match: 'An enrollment administrator lease is active for this user/device.', sentence: 'Someone has temporary admin access on this machine right now. Wait for it to end.' },
  { match: 'Hikvision employee numbers require 1', prefix: true, sentence: 'Employee ID must be 1 to 32 digits.' },
  { match: 'ZKT employee/user IDs cannot exceed 24 characters.', sentence: 'Employee ID can be at most 24 characters.' },
  { match: 'That employee/user ID has already been used on this terminal.', sentence: 'This Employee ID is already used on this machine.' },
  { match: 'The qualified Hikvision profile capacity has been reached.', sentence: 'This machine is full. Remove someone first.' },
  { match: 'The terminal user capacity has been reached.', sentence: 'This machine is full. Remove someone first.' },
  { match: 'The terminal has no never-used 16-bit UID remaining.', sentence: 'This machine is full. Remove someone first.' },
  { match: 'That idempotency key', prefix: true, sentence: 'This request was already sent. Refresh the page.' },
  { match: 'The idempotent create command no longer references a user.', sentence: 'This request was already sent. Refresh the page.' },
  { match: 'The terminal currently encodes this exact CNIC', prefix: true, sentence: 'This CNIC is already used by another Employee ID on this machine.' },
  { match: 'This exact CNIC is reserved by pending terminal user', prefix: true, sentence: 'This CNIC is being added for another Employee ID right now. Wait a moment.' },
  { match: 'Device user is not active.', sentence: 'This person is no longer on the machine. Refresh.' },
  { match: 'Device user not found.', sentence: 'This person is no longer on the machine. Refresh.' },
  { match: 'Target terminal user not found.', sentence: 'This person is no longer on the machine. Refresh.' },
  { match: 'User changed since it was loaded. Refresh and retry.', sentence: 'Someone else changed this person. Refresh and try again.' },
  { match: 'Target user changed since it was selected. Refresh and retry.', sentence: 'Someone else changed this person. Refresh and try again.' },
  { match: 'CNIC cannot be cleared.', sentence: 'You can change the CNIC but not remove it.' },
  { match: 'Hikvision role changes are not qualified on this connector.', sentence: "The admin role can't be changed on this machine." },
  { match: 'Typed confirmation must exactly match', prefix: true, sentence: "The confirmation doesn't match. Type it exactly as shown." },
  { match: 'A reason is required for permanent administrator elevation.', sentence: 'Write a short reason.' },
  { match: 'Demote this permanent administrator before deletion.', sentence: "Machine admins can't be removed. Change their role to Regular first." },
  { match: 'This device already has an active bulk user deletion job.', sentence: 'A removal is already running on this machine.' },
  { match: 'This device already has an active mutating command.', sentence: 'Another change is still being sent to this machine. Wait for it to finish.' },
  { match: 'Lease not found.', sentence: 'This temporary admin access has already ended.' },
  { match: 'Lease target is unavailable.', sentence: 'This temporary admin access has already ended.' },
  { match: 'User deletion job not found.', sentence: "That removal can't be found. Refresh." },
  { match: 'The authenticated connector has not reported a terminal serial.', sentence: "The link box hasn't reported a machine yet. Check the cable." },
  { match: 'CNIC must contain exactly 13 digits.', sentence: 'CNIC must be 13 digits.' },
]

const READ_ONLY = /^This terminal is read-only \(([A-Z0-9_]+)\)\.$/
const USER_CHANGED = /^User \S+ changed since it was selected\.$/
const USER_ACTIVE_COMMAND = /^User \S+ already has an active command\.$/
const USER_PERMANENT_ADMIN = /^User \S+ is a permanent administrator and cannot be deleted\.$/
const USER_GONE = /^(Selected terminal users no longer exist:|User \S+ is no longer active\.)/

/** Plain sentence for a backend People message, or null when it has no words (use plainApiError's fallback). */
export function peopleError(message: string | null | undefined): string | null {
  const text = (message ?? '').trim()
  if (!text) return null
  const readOnly = READ_ONLY.exec(text)
  if (readOnly) return writesOff(readOnly[1]).sentence
  if (USER_CHANGED.test(text)) return 'Someone else changed this person. Refresh and try again.'
  if (USER_ACTIVE_COMMAND.test(text)) return 'Another change is still being sent to this machine. Wait for it to finish.'
  if (USER_PERMANENT_ADMIN.test(text)) return "Machine admins can't be removed. Change their role to Regular first."
  if (USER_GONE.test(text)) return 'Some selected people are no longer on the machine. Refresh.'
  const found = PEOPLE_ERRORS.find((entry) => (entry.prefix ? text.startsWith(entry.match) : text === entry.match))
  return found ? found.sentence : null
}

const SKIP_TITLES = new Set(['dr', 'dr.', 'mr', 'mr.', 'mrs', 'mrs.', 'ms', 'ms.', 'syed', 'muhammad', 'mohammad'])

/** First letters of the first two words, skipping titles (Dr, Mr, Syed, Muhammad …). */
export function personInitials(name: string | null | undefined): string {
  const words = (name ?? '').trim().split(/\s+/).filter(Boolean)
  const kept = words.filter((word) => !SKIP_TITLES.has(word.toLowerCase()))
  const source = kept.length ? kept : words
  return source.slice(0, 2).map((word) => word[0]?.toUpperCase() ?? '').join('') || '?'
}
