import { describe, expect, it } from 'vitest'
import type { Device } from '../types'
import { plainActivity, plainConnectionRow, plainMachineLink } from './machine'

const now = new Date('2026-11-20T10:00:00Z')

const device = (overrides: Partial<Device> = {}) => ({
  connected: true,
  current_activity: 'LIVE_CAPTURE',
  firmware_family: 'zkt' as const,
  hikvision: null,
  last_seen_at: '2026-11-19T15:00:00Z',
  last_error_message: 'reconcile_assignment_release message was rejected (EVIDENCE_INVALID: UNCLASSIFIED_VALUE_ERROR).',
  ...overrides,
}) as Pick<Device, 'connected' | 'current_activity' | 'firmware_family' | 'hikvision' | 'last_seen_at'>

describe('plainActivity (A7, A8)', () => {
  it('never shows a stored activity for an offline link box', () => {
    expect(plainActivity(device({ connected: false }), now)).toBe('Offline since 8:00 pm · 19 h')
  })

  it('translates the stored activity', () => {
    expect(plainActivity(device(), now)).toBe('Recording check-ins')
    expect(plainActivity(device({ current_activity: 'STATE_LOCK_BUSY' }), now)).toBe('Busy, status will update shortly')
    expect(plainActivity(device({ current_activity: null }), now)).toBe('Waiting')
    expect(plainActivity(device({ current_activity: 'SOMETHING_NEW' }), now)).toBe('Working')
  })

  it('reads Hikvision poll errors, never last_error_message', () => {
    const hik = (hikvision: Partial<NonNullable<Device['hikvision']>>) =>
      device({ firmware_family: 'hikvision', hikvision: { capture_mode: 'poll', qualification_state: 'X', ...hikvision } })
    expect(plainActivity(hik({ poll_error: 2 }), now)).toBe('Machine not answering')
    expect(plainActivity(hik({ poll_error: 99 }), now)).toBe('Machine check failing')
    expect(plainActivity(hik({}), now)).toBe('Online · waiting for the first machine check')
    expect(plainActivity(hik({ last_successful_poll_epoch: 1_700_000_000 }), now)).toBe('Recording check-ins')
    expect(plainActivity(hik({ poll_error: 6 }), now)).not.toContain('rejected')
  })
})

describe('plainMachineLink (A4, A5)', () => {
  it('labels the chip and computes the duration in the browser', () => {
    const result = plainMachineLink({ state: 'DISCONNECTED', raw_state: 'RETRY_WAIT', since: '2026-11-20T09:48:00Z', reason: 'RETRY_WAIT', message: 'The terminal has been unreachable for 12 minutes (RETRY_WAIT).' }, now)
    expect(result).toMatchObject({ label: 'Machine not responding', tone: 'problem', duration: 'for 12 min' })
    expect(result.sentence).toBe("Can't reach the machine, waiting to try again.")
    expect(result.technical.message).toContain('RETRY_WAIT')
  })

  it('fills {duration} and falls back for unknown states', () => {
    expect(plainMachineLink({ state: 'DISCONNECTED', raw_state: null, since: '2026-11-20T09:30:00Z', reason: 'MAINTENANCE_OVERRAN', message: null }, now).sentence)
      .toBe('A planned machine restart has gone on for 30 min.')
    expect(plainMachineLink({ state: 'WEIRD', raw_state: null, since: null, reason: 'WEIRD', message: null }, now))
      .toMatchObject({ label: 'Machine status not known', sentence: 'The link box reported a connection state the dashboard has no words for yet.' })
    expect(plainMachineLink(null, now).label).toBe('Machine status not known')
  })
})

describe('plainConnectionRow (A6)', () => {
  it('writes one plain line with counts only above 0', () => {
    expect(plainConnectionRow({ from_state: 'ONLINE', to_state: 'SUSPECT', reason: 'established ZKT session disconnected', consecutive_failures: 3, flap_count_15m: 2 }).sentence)
      .toBe('Lost the machine, trying again · The connection to the machine dropped · 3 failed tries in a row · dropped 2 times in 15 min')
    expect(plainConnectionRow({ from_state: null, to_state: 'ONLINE', reason: 'stable authenticated session', consecutive_failures: 0, flap_count_15m: 0 }).sentence)
      .toBe('First record: Connected to the machine · The connection has been steady')
  })

  it('keeps unknown reason text out of the sentence', () => {
    const result = plainConnectionRow({ from_state: 'ONLINE', to_state: 'RETRY_WAIT', reason: 'socket errno=104 on fd 7', consecutive_failures: 1, flap_count_15m: 0 })
    expect(result.reason).toBe('')
    expect(result.sentence).not.toContain('errno')
  })
})
