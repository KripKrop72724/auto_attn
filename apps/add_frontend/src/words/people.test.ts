import { describe, expect, it } from 'vitest'
import { findPlainWordProblems } from './glossary'
import {
  ADMIN_ACCESS, CHANGES, HEADER, LIST, MACHINE_CHECK, NOTICE, OLD_CHECKINS, PERSON_DIALOG, PICKER, PROBLEM_HELP, REMOVE_MANY,
  SAME_CNIC, WRITES_OFF, changeDone, changeLabel, changeLine, personInitials, personProblems, removalHeading, removalItem, writesOff,
} from './people'

// Every writes_disabled_reason the backend sets (be/service.py, be/web.py) plus the
// service's own fallback code (DEVICE_NOT_WRITE_CERTIFIED).
const BACKEND_WRITE_OFF_CODES = [
  'DUPLICATE_SERIAL', 'RECOVERY_SERIAL_PENDING_PROOF', 'SERIAL_MISMATCH', 'STABILITY_CERTIFICATION_PENDING',
  'TERMINAL_SERIAL_CONFIRMATION_REQUIRED', 'TERMINAL_SERIAL_PENDING_DEVICE_ACK', 'TERMINAL_SERIAL_PIN_FAILED',
  'USER_SNAPSHOT_TRUNCATED', 'HIKVISION_WRITE_QUALIFICATION_PENDING', 'LEGACY_28_BYTE_RECORD',
  'FULL_USER_SNAPSHOT_REQUIRED', 'DEVICE_NOT_WRITE_CERTIFIED',
]

const counts = { requested: 3, succeeded: 2, failed: 1, pending: 0, canceled: 0, expired: 0 }

/** Every string (and every function called with sample values) in a words object. */
function strings(value: unknown): string[] {
  if (typeof value === 'string') return [value]
  if (typeof value === 'function') {
    const out = (value as (...args: unknown[]) => unknown)(3, 'Building 9 · Device 3', 'Ayesha Khan')
    return typeof out === 'string' ? [out] : []
  }
  if (Array.isArray(value)) return value.flatMap(strings)
  if (value && typeof value === 'object') return Object.values(value).flatMap(strings)
  return []
}

describe('People words', () => {
  it('has a sentence for every reason changes are off', () => {
    for (const code of BACKEND_WRITE_OFF_CODES) {
      expect(writesOff(code).unknown, code).toBe(false)
      expect(WRITES_OFF[code]).toBeTruthy()
    }
    expect(writesOff('SOMETHING_NEW')).toEqual({ sentence: 'Changes are off for this machine right now. Ask IT.', unknown: true })
  })

  it('has words for every lease state, removal status and item status', () => {
    for (const state of ['REQUESTED', 'GRANTING', 'REVOKING', 'OVERDUE', 'FAILED', 'REVOKED', 'EXPIRED']) expect(ADMIN_ACCESS.states[state], state).toBeTruthy()
    for (const status of ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'EXPIRED', 'CANCELED']) {
      expect(removalHeading(status, counts)).not.toBe('Removing people')
    }
    expect(removalHeading('PARTIAL', counts)).toBe('Finished with problems: 2 removed, 1 not removed')
    expect(removalItem('SUCCEEDED')).toBe('removed')
    expect(removalItem('FAILED', "The machine didn't confirm it")).toBe("not removed: The machine didn't confirm it")
    expect(removalItem('CANCELED')).toBe('skipped')
    expect(removalItem('EXPIRED')).toBe('not done in time')
    expect(removalItem('PENDING')).toBe('waiting')
  })

  it('has words for every Same CNIC classification and advice, and every old check-in path', () => {
    for (const code of ['EXACT_NAME_MATCH', 'POSSIBLE_NAME_VARIANT', 'MIXED_NAMES_HIGH_RISK']) expect(SAME_CNIC.classification[code]).toBeTruthy()
    for (const code of ['CONFIRM_SAME_EMPLOYEE', 'HR_IDENTITY_REVIEW']) expect(SAME_CNIC.advice[code]).toBeTruthy()
    for (const code of ['HR_DIRECTORY_EVIDENCE', 'HR_DIRECTORY_EVENT_GROUP', 'ACTIVE_USER_ENRICHMENT', 'CURRENT_IDENTITY_EVIDENCE', 'VERIFIED_TOMBSTONE_REPAIR', 'IDENTITY_CONFLICT_REVIEW', 'IDENTITY_REUSE_REVIEW']) {
      expect(OLD_CHECKINS.paths[code], code).toBeTruthy()
    }
  })

  it('labels every change and every change status', () => {
    for (const type of ['CREATE_USER', 'UPDATE_USER', 'DELETE_USER', 'GRANT_TEMP_ADMIN', 'REVOKE_TEMP_ADMIN', 'REFRESH_USERS', 'PIN_TERMINAL_SERIAL']) {
      expect(changeLabel(type, 'Ayesha Khan')).not.toBe('Changing the machine')
      expect(changeDone(type, 'Ayesha Khan')).not.toBe('change made')
    }
    expect(changeLabel('CREATE_USER', 'Ayesha Khan')).toBe('Adding Ayesha Khan')
    expect(changeLine('SUCCEEDED', '', '10:44 am')).toBe('Confirmed on the machine at 10:44 am.')
    expect(changeLine('EXPIRED', '', '')).toBe("Failed: the machine didn't answer in time. Nothing was changed.")
  })

  it('writes one plain chip per fact', () => {
    expect(personProblems({ cnic_available: false }).map((chip) => chip.label)).toEqual(['No CNIC'])
    expect(personProblems({ cnic_available: false, cnic_format_suspect: true }).map((chip) => chip.label)).toEqual(['CNIC typed wrongly'])
    expect(personProblems({ cnic_available: true, identity_conflict_code: 'DUP', identity_conflict_members: [{ user_id: '1008' }] }).map((chip) => chip.label)).toEqual(['Same CNIC as 1008'])
    expect(personProblems({ cnic_available: true, identity_conflict_code: 'DUP', identity_conflict_members: ['1008', '1009', '1010'] })[0].label).toBe('Same CNIC as 1008 and 2 more')
    expect(personProblems({ cnic_available: true, privilege: 14 }).map((chip) => chip.label)).toEqual(['Machine admin'])
    expect(personProblems({ cnic_available: true, looks_damaged: true }).map((chip) => chip.label)).toEqual(['Damaged entry'])
  })

  it('makes initials without titles', () => {
    expect(personInitials('Ayesha Khan')).toBe('AK')
    expect(personInitials('Dr Muhammad Ali Raza')).toBe('AR')
    expect(personInitials('Syed')).toBe('S')
  })

  it('counts people with real plurals', () => {
    expect(PICKER.people(1)).toBe('1 person')
    expect(PICKER.people(2)).toBe('2 people')
    expect(REMOVE_MANY.title(1, 'Building 9 · Device 3')).toBe('Remove 1 person from Building 9 · Device 3?')
    expect(REMOVE_MANY.someLeft(2)).toBe("2 people can't be removed any more and were taken off your list.")
  })

  it('passes the plain-words guard everywhere', () => {
    const all = [PICKER, HEADER, NOTICE, MACHINE_CHECK, LIST, PERSON_DIALOG, CHANGES, ADMIN_ACCESS, SAME_CNIC, OLD_CHECKINS, REMOVE_MANY, PROBLEM_HELP, WRITES_OFF].flatMap(strings)
    expect(all.length).toBeGreaterThan(200)
    const problems = all.flatMap((text) => findPlainWordProblems(text).map((problem) => `${problem.why}: "${problem.match}" in "${text}"`))
    expect(problems).toEqual([])
  })
})
