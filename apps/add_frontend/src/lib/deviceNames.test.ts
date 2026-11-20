import { describe, expect, it } from 'vitest'
import fixture from './deviceNames.fixture.json'
import { deviceName, friendlyNames } from './deviceNames'
import { deviceDivision, inDivision, parseDivision } from './division'

// The same file is checked against the backend by
// tests/unit/test_device_names_fixture.py, so these two rules cannot drift.
describe('device names mirror the backend', () => {
  it.each(fixture.name_cases)('$code reads as $zone · $location · $unit', (item) => {
    expect(deviceName(item.code)).toEqual({ zone: item.zone, location: item.location, unit: item.unit, number: item.number })
  })

  it('names the 20 live active devices exactly as /status does', () => {
    expect(friendlyNames(fixture.live.active)).toEqual(fixture.live.names)
  })

  it('splits the 24 live names (spares included) into 9 Life and 15 Health', () => {
    const all = [...fixture.live.active, ...fixture.live.spare]
    expect(all).toHaveLength(24)
    expect(all.filter((code) => deviceDivision({ display_name: code }) === 'life')).toHaveLength(9)
    expect(all.filter((code) => deviceDivision({ display_name: code }) === 'health')).toHaveLength(15)
  })
})

describe('division rule', () => {
  it.each([
    ['LF-ZONE-BLD9-03', 'life'],
    ['LF ZONE BLD1', 'life'],
    ['lf-zone-x', 'life'],
    ['LFZONE-1', 'health'],
    ['ZONE-SWAT-01', 'health'],
    ['ZONE-SLICTOWER-13FL', 'health'],
    ['', 'health'],
  ] as const)('%s is %s', (code, division) => {
    expect(deviceDivision({ display_name: code })).toBe(division)
  })

  it('prefers the division the backend sends, then the name, then the zone', () => {
    expect(deviceDivision({ division: 'life', display_name: 'ZONE-SWAT-01' })).toBe('life')
    expect(deviceDivision({ division: null, display_name: 'LF-ZONE-BLD1-01' })).toBe('life')
    expect(deviceDivision({ display_name: '', zone_id: 'LF-ZONE-BLD5-01' })).toBe('life')
    expect(deviceDivision({ division: 'unknown', display_name: 'ZONE-SWAT-01' })).toBe('health')
  })

  it('reads only life and health from the URL', () => {
    expect(parseDivision('?division=life')).toBe('life')
    expect(parseDivision('?division=health&tab=x')).toBe('health')
    expect(parseDivision('?division=LIFE')).toBe('all')
    expect(parseDivision('')).toBe('all')
    expect(inDivision({ display_name: 'LF-ZONE-BLD1-01' }, 'all')).toBe(true)
    expect(inDivision({ display_name: 'LF-ZONE-BLD1-01' }, 'health')).toBe(false)
  })
})
