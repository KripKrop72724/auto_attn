import { describe, expect, it } from 'vitest'
import { deviceStatus, isOfflineLikeStatus, OFFLINE_REASONS, type DeviceStatusInput } from './deviceStatus'
import { findPlainWordProblems } from './glossary'

const link = (state: string, reason: string | null = null, since: string | null = '2026-11-20T04:00:00Z') => ({
  state, reason, since, raw_state: null, message: 'The terminal link is up.',
})

const row = (overrides: Partial<DeviceStatusInput> = {}): DeviceStatusInput => ({
  state: 'ONLINE',
  connected: true,
  last_seen_at: '2026-11-20T05:00:00Z',
  terminal_link: link('CONNECTED', 'LINK_UP'),
  ...overrides,
})

describe('deviceStatus (HOME-7, D-1)', () => {
  it('reads Working for a healthy device', () => {
    expect(deviceStatus(row())).toMatchObject({ group: 'working', chip: 'Working', tone: 'good', note: null })
  })

  it('checks Spare before Offline', () => {
    expect(deviceStatus(row({ is_spare: true, state: 'OFFLINE', connected: false }))).toMatchObject({ group: 'spare', chip: 'Spare (not in use)' })
  })

  it('reads Blocked: duplicate device and counts it under Needs attention', () => {
    expect(deviceStatus(row({ state: 'QUARANTINED_DUPLICATE_SERIAL' }))).toMatchObject({ group: 'attention', chip: 'Blocked: duplicate device', tone: 'problem' })
  })

  it('reads Offline · No internet when the link box is not connected', () => {
    const result = deviceStatus(row({ state: 'OFFLINE', connected: false }))
    expect(result).toMatchObject({ group: 'offline', chip: 'Offline', heading: 'No internet', since: '2026-11-20T05:00:00Z' })
    expect(result.sentence).toBe(OFFLINE_REASONS.no_internet.sentence)
  })

  it('D-1: a machine that is not responding counts as Offline, exactly like /status', () => {
    expect(deviceStatus(row({ terminal_link: link('DISCONNECTED', 'HIK_NETWORK') }))).toMatchObject({
      group: 'offline', chip: 'Offline', heading: 'Machine not responding', code: 'machine_not_responding',
    })
    expect(isOfflineLikeStatus(row({ terminal_link: link('DISCONNECTED') }))).toBe(true)
  })

  it('D-1: no machine linked and machine errors are Offline with the /status reasons', () => {
    expect(deviceStatus(row({ terminal_link: link('UNKNOWN', 'NO_TERMINAL') }))).toMatchObject({ group: 'offline', heading: 'Not set up yet' })
    expect(deviceStatus(row({ terminal_link: link('ERROR', 'HIK_AUTH') }))).toMatchObject({ group: 'offline', heading: 'Wrong machine settings' })
    expect(deviceStatus(row({ terminal_link: link('ERROR', 'HIK_SOURCE_CHANGED') }))).toMatchObject({ heading: 'Machine records changed' })
    expect(deviceStatus(row({ terminal_link: link('ERROR', 'HIK_STORAGE') }))).toMatchObject({ heading: 'Machine problem' })
  })

  it('uses the backend public_status answer when the row carries it (grace periods match /status)', () => {
    expect(deviceStatus(row({ public_status: { status: 'offline', since: '2026-11-19T12:00:00Z', reason_code: 'machine_not_responding' } })))
      .toMatchObject({ group: 'offline', since: '2026-11-19T12:00:00Z', heading: 'Machine not responding' })
    // /status hasn't counted it yet: Needs attention meanwhile, same words.
    expect(deviceStatus(row({ public_status: { status: 'online' }, terminal_link: link('DISCONNECTED') })))
      .toMatchObject({ group: 'attention', heading: 'Machine not responding' })
    // Same for no internet: /status still says Online, so it is never Offline here yet.
    expect(deviceStatus(row({ public_status: { status: 'online' }, connected: false })))
      .toMatchObject({ group: 'attention', heading: 'No internet' })
    expect(deviceStatus(row({ public_status: { status: 'online' }, state: 'OFFLINE' })))
      .toMatchObject({ group: 'attention', heading: 'No internet' })
    expect(deviceStatus(row({ public_status: { status: 'offline', since: '2026-11-19T12:00:00Z', reason_code: 'no_internet' }, connected: false })))
      .toMatchObject({ group: 'offline', since: '2026-11-19T12:00:00Z', heading: 'No internet' })
  })

  it('keeps FLAPPING and DEGRADED in Needs attention', () => {
    expect(deviceStatus(row({ terminal_link: link('FLAPPING', 'FLAPPING') }))).toMatchObject({ group: 'attention', heading: 'Machine connection keeps dropping' })
    expect(deviceStatus(row({ state: 'DEGRADED', health: { tier: 'DEGRADED', primary: { code: 'ESP_DURABILITY_FAULT', error_code: null, tier: 'DEGRADED', message: 'storage DEGRADED; 0.0% used', since: null } } as DeviceStatusInput['health'] })))
      .toMatchObject({ group: 'attention', chip: 'Needs attention', heading: 'Link box cannot save attendance safely' })
  })

  it('prefers the backend plain title and never shows the backend message', () => {
    const health = { tier: 'DEGRADED', primary: { code: 'ESP_RESTART_LOOP', error_code: null, tier: 'DEGRADED', message: 'ESP restart loop', since: null, plain_title: 'Link box keeps restarting (6 times in 10 min)' } }
    const result = deviceStatus(row({ state: 'DEGRADED', health: health as unknown as DeviceStatusInput['health'] }))
    expect(result.heading).toBe('Link box keeps restarting (6 times in 10 min)')
    expect(JSON.stringify(result)).not.toContain('ESP restart loop')
  })

  it('an unknown primary code reads "Needs attention" with "Open the device for details."', () => {
    const health = { tier: 'DEGRADED', primary: { code: 'BRAND_NEW_CODE', error_code: null, tier: 'DEGRADED', message: 'x', since: null } }
    expect(deviceStatus(row({ state: 'DEGRADED', health: health as unknown as DeviceStatusInput['health'] }))).toMatchObject({ heading: 'Needs attention', sentence: 'Open the device for details.' })
  })

  it('Working with a note shows a grey note, not a different chip', () => {
    const health = { tier: 'ONLINE_WITH_WARNINGS', primary: { code: 'OTA_DEVICE_REPORTED_FAILURE', error_code: null, tier: 'WARNING', message: 'x', since: null } }
    expect(deviceStatus(row({ state: 'ONLINE_WITH_WARNINGS', health: health as unknown as DeviceStatusInput['health'] })))
      .toMatchObject({ chip: 'Working', note: 'Note: Software update failed.' })
  })

  it('every heading and sentence passes the plain-words guard', () => {
    for (const reason of Object.values(OFFLINE_REASONS)) {
      expect(findPlainWordProblems(reason.heading)).toEqual([])
      expect(findPlainWordProblems(reason.sentence)).toEqual([])
    }
  })
})

// The Offline reasons are checked word for word against be/public_status.py REASONS
// by tests/unit/test_word_codes.py (vite does not serve files outside the app).
