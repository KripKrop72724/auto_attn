import { describe, expect, it } from 'vitest'
import groupsFixture from './deliveryGroups.fixture.json'
import { findPlainWordProblems } from '../../words/glossary'
import {
  arrivalWords, checkinKind, clockWords, explainReleaseLock, GROUP_CHIP, GROUP_ORDER, knownDeliveryCodes, neverSentSentence,
  PHONE_STATUS_WORDS, plainError, rowGroup, rowTime, SKIP_WORDS, statusNote,
} from './attendanceWords'
import type { AttendanceRow } from './types'

const row = (overrides: Partial<AttendanceRow> = {}): AttendanceRow => ({
  id: 1, event_uid: 'e', device_serial: 'S1', uid: '7', user_id: '1007', display_name: 'Ayesha Khan', cnic_masked: '*****-****567-1',
  device_event_time: '2026-11-06T03:02:00Z', captured_at: '2026-11-06T03:02:01Z', received_at: '2026-11-06T03:02:02Z',
  source: 'LIVE', status: '0', punch: '0', clock_quality: 'OK', clock_drift_seconds: 0, ords_status: 'PENDING',
  oracle_confirmed_at: null, oracle_confirmation_path: null, identity_resolution_id: null, ...overrides,
})

const plain = (text: string) => expect(findPlainWordProblems(text), text).toEqual([])

describe('attendance words', () => {
  it('maps every stored status to the same group as the server (shared fixture)', () => {
    for (const item of groupsFixture.rows as Array<{ ords_status: string | null; delivery_group: string }>) {
      expect(rowGroup({ ords_status: item.ords_status ?? '', delivery_group: null }), String(item.ords_status)).toBe(item.delivery_group)
    }
    expect(rowGroup({ ords_status: 'SOMETHING_NEW', delivery_group: null })).toBe('problem')
    // The server's own group wins when it is sent.
    expect(rowGroup({ ords_status: 'PENDING', delivery_group: 'sent' })).toBe('sent')
    expect(GROUP_ORDER).toEqual(groupsFixture.groups)
  })

  it('gives every status, arrival, clock and phone state plain words', () => {
    for (const code of [...knownDeliveryCodes(), 'UNKNOWN_CODE']) {
      const value = row({ ords_status: code, delivery_group: null })
      plain(GROUP_CHIP[rowGroup(value)].label)
      plain(statusNote(value, true))
      plain(statusNote(value, false))
      const never = neverSentSentence(value)
      if (never) plain(never)
    }
    for (const exclusion of ['CNIC_CONFLICT', 'IDENTITY_CONFLICT_UNRESOLVED', 'CLOCK_INVALID', 'TIME_IMPOSSIBLE', 'INVALID_EVENT_UID', 'CNIC_MISSING'] as const) {
      plain(statusNote(row({ ords_status: 'BLOCKED_IDENTITY', direct_ords_identity: { eligible: false, cnic_source: null, exclusion } })))
    }
    for (const source of ['LIVE', 'LIVE_POLL', 'DUMP_RECONNECT', 'DUMP_STARTUP', 'FULL_HISTORY', 'CURRENT_RECONCILE', 'RECONCILE_15M', 'OTHER']) plain(arrivalWords(source))
    for (const quality of ['OK', 'DRIFTED', 'UNKNOWN', 'INVALID', 'OPERATOR_CORRECTED', '']) plain(clockWords(quality, 125))
    Object.values(PHONE_STATUS_WORDS).forEach(plain)
    Object.values(SKIP_WORDS).forEach(plain)
    for (const code of ['TARGET_CNIC_MISSING', 'CLOCK_NOT_OK', 'SOURCE_RECERTIFICATION_REQUIRED', 'NEW_CODE']) plain(explainReleaseLock(code))
    expect(explainReleaseLock('NEW_CODE')).toBe("Can't be sent (see code)")
  })

  it('writes the status notes from the plan', () => {
    const now = Date.parse('2026-11-06T05:00:00Z')
    expect(statusNote(row({ ords_status: 'ACKED', oracle_confirmed_at: '2026-11-06T03:03:00Z' }), true, now)).toBe('Sent 8:03 am')
    expect(statusNote(row({ ords_status: 'IN_FLIGHT' }))).toBe('Sending now')
    expect(statusNote(row({ ords_status: 'FAILED_RETRYABLE' }))).toBe('Trying again')
    expect(statusNote(row({ ords_status: 'ACKED_FIRMWARE' }))).toBe('Checking with the HR system')
    expect(statusNote(row({ ords_status: 'PENDING' }))).toBe('Will be sent automatically')
    const synced = row({ display_name: null, cnic_masked: null, ords_status: 'BLOCKED_IDENTITY', direct_ords_identity: { eligible: true, cnic_source: 'SYNCED_USER', exclusion: null } })
    expect(statusNote(synced, true)).toBe('Not matched · CNIC found on the machine, sending by itself')
    expect(statusNote(synced, false)).toBe('Not matched · CNIC found on the machine, press Send')
    expect(statusNote(row({ ords_status: 'QUARANTINED_INVALID_DEVICE_TIME' }))).toBe('Wrong time on the machine · not sent until the time is fixed')
    expect(statusNote(row({ ords_status: 'QUARANTINED_ORDS_REJECTED' }))).toBe('The HR system refused it')
    expect(statusNote(row({ ords_status: 'BLOCKED_IDENTITY', device_serial: null }))).toBe('Machine not confirmed')
  })

  it('writes times and kinds the way people read them', () => {
    const now = Date.parse('2026-11-06T05:00:00Z')
    expect(rowTime('2026-11-06T03:02:00Z', now)).toBe('8:02 am')
    expect(rowTime('2026-11-05T12:12:00Z', now)).toBe('Yesterday, 5:12 pm')
    expect(rowTime('2026-11-03T03:00:00Z', now)).toBe('Tue 3 Nov, 8:00 am')
    expect(checkinKind('0')).toBe('Check-in')
    expect(checkinKind('1')).toBe('Check-out')
    expect(checkinKind('4')).toBe('Other type (4)')
    expect(checkinKind(null)).toBe('Type not recorded')
    expect(clockWords('DRIFTED', 125)).toBe('Machine time was off by 2 min')
  })

  it('never shows a raw error', () => {
    expect(plainError({ status: 503 })).toBe("The server didn't answer. Try again in a minute.")
    expect(plainError({ status: 409, code: 'ADD_BUSY' })).toBe('Busy · trying again…')
    expect(plainError(new Error('ApiError: boom'))).toBe("Couldn't do this. Try again.")
  })
})
