import { describe, expect, it } from 'vitest'
import { deliveryCodes, deliveryGroup, isSentToHr, checkinType } from './attendance'
import { plainReason } from './attendanceReasons'
import { errorWords, ERROR_FALLBACK } from './errors'
import { plainReasonLine, plainTier, whyTitle } from './health'
import { peopleError, personInitials, personProblems, writesOff } from './people'
import { problemWords, refusalWords, resolutionWords } from './problems'
import { requestEndLabel, requestStep, requestTitle } from './requests'
import { setupMessage } from './setup'

describe('problem words (Appendix C)', () => {
  it('prefers the backend plain fields and never uses the message', () => {
    expect(problemWords({ code: 'ESP_RESTART_LOOP', plain_title: 'Link box keeps restarting (6 times in 10 min)' }))
      .toMatchObject({ title: 'Link box keeps restarting (6 times in 10 min)', groupLabel: 'Link box', closesWhen: 'Closes by itself when it stays on for 30 minutes.' })
  })

  it('falls back by code, then by group', () => {
    expect(problemWords({ code: 'ESP_DURABILITY_FAULT' }).title).toBe('Link box cannot save attendance safely')
    expect(problemWords({ code: 'ORDS_SOMETHING_NEW' })).toMatchObject({ title: 'HR system problem', next: 'Open the device for details.', unknown: true })
    expect(problemWords({ code: 'NEW' }).title).toBe('Link box problem')
  })

  it('says how a problem was fixed and why it cannot be marked fixed', () => {
    expect(resolutionWords('OPERATOR', 'StateHealthAdmin')).toBe('Marked fixed by StateHealthAdmin')
    expect(resolutionWords('CLEANUP')).toBe('Closed by the old-problems clean-up')
    expect(resolutionWords('WHO_KNOWS')).toBe('Fixed by itself')
    expect(refusalWords('ALERT_CONDITION_CURRENT', 'ESP_PRESERVATION_UNVERIFIED')).toBe('Closes after a software update that can check the link box memory.')
    expect(refusalWords('ALERT_CONDITION_CURRENT', 'X', 'HELD')).toBe('Waiting for the link box to confirm it since its last restart.')
    expect(refusalWords('NEW')).toBe("This one can't be marked as fixed by hand.")
  })

  it('health tier pills and headings', () => {
    expect(plainTier('DEGRADED').label).toBe('Needs attention')
    expect(plainTier('WARNING').label).toBe('Note')
    expect(plainTier(null).label).toBe('For information')
    expect(whyTitle('ONLINE_WITH_WARNINGS')).toBe('Why this device is working with a note')
    expect(plainReasonLine({ code: 'TERMINAL_DISCONNECTED', since: '2026-11-20T04:24:00Z' }, new Date('2026-11-20T08:00:00Z')))
      .toBe('Machine not responding · since 9:24 am')
  })
})

describe('request words (Appendix E)', () => {
  it('titles, steps and endings', () => {
    expect(requestTitle('PIN_TERMINAL_SERIAL')).toBe("Confirm the machine's serial number")
    expect(requestTitle('NEW_TYPE')).toBe('Request')
    expect(requestStep('QUEUED')).toBe(1)
    expect(requestStep('WAITING_FOR_ZKT')).toBe(2)
    expect(requestStep('SUCCEEDED')).toBe(3)
    expect(requestEndLabel('EXPIRED')).toBe('Timed out')
  })
})

describe('attendance words (Appendices D and H)', () => {
  it('counts only ACKED and ACKED_CHECK as sent', () => {
    expect(isSentToHr('ACKED')).toBe(true)
    expect(isSentToHr('ACKED_FIRMWARE')).toBe(false)
    expect(deliveryGroup('BLOCKED_IDENTITY')).toBe('not_matched')
    expect(deliveryGroup('WHATEVER')).toBe('problem')
    expect(deliveryCodes('sent')).toEqual(['ACKED', 'ACKED_CHECK'])
    expect(checkinType(0)).toBe('Check-in')
    expect(checkinType(5)).toBe('Other (5)')
  })

  it('maps reason codes and keeps the backend sentence for Technical details only', () => {
    expect(plainReason('direct', 'SYNC_EXPIRED', 'Sync Expired')).toMatchObject({ sentence: 'This check-in needs a closer look before it can be sent.', technical: 'Sync Expired', unknown: true })
    expect(plainReason('force', 'SYNC_EXPIRED').sentence).toBe('Re-reading the list of people took too long. Try this device again later.')
    expect(plainReason('skip', 'NEW').sentence).toBe('skipped for safety')
    expect(plainReason('areaWait', 'WAITING_FOR_COMPLETE_ROSTER').sentence).toContain('complete list of people from a chosen machine')
    expect(plainReason('recoveryItem', null).sentence).toBe('Waiting its turn')
    expect(plainReason('lock', 'PREVIEW_DISABLED').sentence).toBe('Releasing check-ins is switched off for now.')
  })

  it('maps refusal codes; unknown codes get the safe sentence', () => {
    expect(errorWords('PREVIEW_EXPIRED')).toEqual({ sentence: 'This check has expired. Check again.', unknown: false, action: 'Check again' })
    expect(errorWords('NEW_CODE')).toEqual({ sentence: ERROR_FALLBACK, unknown: true, action: 'Try again' })
  })
})

describe('people and setup words', () => {
  it('people chips, writes-off reasons, errors and initials', () => {
    expect(personProblems({ cnic_available: false })).toEqual([{ key: 'no_cnic', label: 'No CNIC', tone: 'warning' }])
    expect(personProblems({ identity_conflict_code: 'X', identity_conflict_members: [1008, 1009, 1010] })[0].label).toBe('Same CNIC as 1008 and 2 more')
    expect(writesOff('USER_SNAPSHOT_TRUNCATED').unknown).toBe(false)
    expect(writesOff('NEW').sentence).toBe('Changes are off for this machine right now. Ask IT.')
    expect(peopleError('This terminal is read-only (SERIAL_MISMATCH).')).toContain('serial number')
    expect(peopleError('User 1007 changed since it was selected.')).toBe('Someone else changed this person. Refresh and try again.')
    expect(peopleError('Something new')).toBeNull()
    expect(personInitials('Dr Syed Ayesha Khan')).toBe('AK')
  })

  it('setup server messages', () => {
    expect(setupMessage('Physical provisioning is disabled.')).toBe('Setting up link boxes from the dashboard is switched off.')
    expect(setupMessage('You already have active session 1234.')).toBe('You already have a setup running. Finish or cancel it first.')
    expect(setupMessage('FIRMWARE_FAMILY_MISMATCH')).toContain('machine brand')
    expect(setupMessage('nope')).toBeNull()
  })
})

// codes.generated.json is for tests only: app code must never import it (bundle budget).
const sources = import.meta.glob('../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }) as Record<string, string>

describe('words sources', () => {
  it('app code never imports codes.generated.json', () => {
    const offenders = Object.entries(sources)
      .filter(([path]) => !/\.test\.tsx?$/.test(path))
      .filter(([, text]) => text.includes('codes.generated.json'))
      .map(([path]) => path)
    expect(offenders).toEqual([])
  })

  it('words modules never import feature pages', () => {
    const offenders = Object.entries(sources)
      .filter(([path]) => path.startsWith('../words/') && !/\.test\.tsx?$/.test(path))
      .filter(([, text]) => /from '\.\.\/features\//.test(text))
      .map(([path]) => path)
    expect(offenders).toEqual([])
  })
})
