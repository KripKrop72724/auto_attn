import { describe, expect, it } from 'vitest'
import { alertStatePattern, isOnlineState, statusPattern, terminalLinkNeedsAttention, terminalLinkPattern } from './status'

describe('device health status patterns', () => {
  it('renders online with warnings as a notice, distinct from degraded', () => {
    expect(statusPattern('ONLINE_WITH_WARNINGS')).toBe('notice')
    expect(statusPattern('online_with_warnings')).toBe('notice')
    expect(statusPattern('DEGRADED')).toBe('waiting')
    expect(statusPattern('WARNING')).toBe('waiting')
    expect(statusPattern('ONLINE')).toBe('confirmed')
    expect(statusPattern('OFFLINE')).toBe('blocked')
    expect(statusPattern('QUARANTINED_DUPLICATE_SERIAL')).toBe('blocked')
  })

  it('keeps the global ACKNOWLEDGED mapping for non-alert workflow statuses', () => {
    expect(statusPattern('ACKNOWLEDGED')).toBe('confirmed')
    expect(statusPattern('ORDS_ACKED')).toBe('confirmed')
  })

  it('counts both online tiers as online', () => {
    expect(isOnlineState('ONLINE')).toBe(true)
    expect(isOnlineState('ONLINE_WITH_WARNINGS')).toBe(true)
    expect(isOnlineState(' online ')).toBe(true)
    for (const state of ['DEGRADED', 'OFFLINE', 'FLAPPING', 'ONBOARDING', 'QUARANTINED_DUPLICATE_SERIAL', null, undefined, '']) {
      expect(isOnlineState(state)).toBe(false)
    }
  })

  it.each([
    ['CONNECTED', 'confirmed'],
    ['STABILIZING', 'notice'],
    ['MAINTENANCE', 'notice'],
    ['STARTING', 'notice'],
    ['UNKNOWN', 'notice'],
    ['RECONNECTING', 'waiting'],
    ['FLAPPING', 'waiting'],
    ['DISCONNECTED', 'blocked'],
    ['ERROR', 'blocked'],
    ['SOMETHING_NEW', 'notice'],
    [null, 'notice'],
  ])('maps terminal link %s to %s', (state, pattern) => {
    expect(terminalLinkPattern(state)).toBe(pattern)
  })

  it('flags only down terminal links for attention', () => {
    expect(['DISCONNECTED', 'FLAPPING', 'ERROR'].every(terminalLinkNeedsAttention)).toBe(true)
    expect(['CONNECTED', 'STABILIZING', 'MAINTENANCE', 'STARTING', 'RECONNECTING', 'UNKNOWN'].some(terminalLinkNeedsAttention)).toBe(false)
    expect(terminalLinkNeedsAttention(undefined)).toBe(false)
  })

  it('styles acknowledged alerts as notices and resolved alerts as confirmed', () => {
    expect(alertStatePattern({ state: 'OPEN', severity: 'HIGH', acknowledged_at: '2026-10-10T08:00:00Z' })).toBe('notice')
    expect(alertStatePattern({ state: 'ACKNOWLEDGED', severity: 'CRITICAL', acknowledged_at: null })).toBe('notice')
    expect(alertStatePattern({ state: 'RESOLVED', severity: 'CRITICAL', acknowledged_at: '2026-10-10T08:00:00Z' })).toBe('confirmed')
    expect(alertStatePattern({ state: 'OPEN', severity: 'CRITICAL', acknowledged_at: null })).toBe('blocked')
    expect(alertStatePattern({ state: 'OPEN', severity: 'HIGH', acknowledged_at: null })).toBe('blocked')
    expect(alertStatePattern({ state: 'OPEN', severity: 'WARNING', acknowledged_at: null })).toBe('waiting')
  })
})
