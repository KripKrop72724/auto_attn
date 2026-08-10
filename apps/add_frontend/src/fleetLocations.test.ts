import { describe, expect, it } from 'vitest'
import { groupFleetLocations, resolveFleetLocation } from './fleetLocations'
import type { Device } from './types'

const device = (overrides: Partial<Device> = {}): Device => ({
  connector_id: 'connector-one',
  hardware_id: 'e0:72:a1:d6:f3:28',
  zone_id: 'ZONE-SLICTOWER-3FL',
  zone_name: 'SLICTOWER',
  device_id: '1',
  display_name: 'SLICTOWER · 3rd Floor',
  state: 'ONLINE',
  connected: true,
  firmware_version: '2.4.5',
  onboarding_generation: 2,
  last_onboarded_at: null,
  last_seen_at: '2026-08-10T10:00:00Z',
  current_activity: 'LIVE_CAPTURE',
  last_error_code: null,
  zkt: null,
  ...overrides,
})

describe('fleet location resolution', () => {
  it.each([
    ['ZONE-KARACHI-01', 'Karachi'],
    ['ZONE-PESHAWAR-02', 'Peshawar'],
    ['ZONE-PESH-01', 'Peshawar'],
    ['ZONE-SWAT-01', 'Swat'],
    ['ZONE-SLICTOWER-13FL', 'Islamabad'],
    ['SLIC-TOWER-11-FLOOR', 'Islamabad'],
    ['Slic Tower Head Office', 'Islamabad'],
  ])('maps %s to %s', (zoneId, city) => {
    expect(resolveFleetLocation({ zone_id: zoneId, zone_name: zoneId, display_name: zoneId })?.city).toBe(city)
  })

  it('keeps unknown locations explicit', () => {
    expect(resolveFleetLocation({ zone_id: 'ZONE-LAHORE-01', zone_name: 'Punjab', display_name: 'Branch 1' })).toBeNull()
  })
})

describe('fleet location aggregation', () => {
  it('groups co-located devices and uses the worst operational state', () => {
    const result = groupFleetLocations([
      device({ connector_id: 'tower-3', zone_id: 'ZONE-SLICTOWER-3FL', state: 'ONLINE' }),
      device({ connector_id: 'tower-13', zone_id: 'ZONE-SLICTOWER-13FL', state: 'DEGRADED', last_seen_at: null }),
      device({ connector_id: 'peshawar', zone_id: 'ZONE-PESHAWAR-02', zone_name: 'Peshawar', display_name: 'Peshawar', state: 'OFFLINE' }),
    ])
    const islamabad = result.groups.find((group) => group.definition.id === 'islamabad')
    const peshawar = result.groups.find((group) => group.definition.id === 'peshawar')
    expect(islamabad).toMatchObject({ total: 2, online: 1, attention: 1, pattern: 'waiting' })
    expect(islamabad?.lastSeenAt).toBe('2026-08-10T10:00:00Z')
    expect(peshawar).toMatchObject({ total: 1, online: 0, attention: 1, pattern: 'blocked' })
  })

  it('preserves unknown and nullable device data without inventing a pin', () => {
    const result = groupFleetLocations([
      device({ connector_id: 'unknown', zone_id: 'ZONE-UNKNOWN', zone_name: '', display_name: 'New branch', state: 'UNEXPECTED', last_seen_at: null }),
    ])
    expect(result.groups).toEqual([])
    expect(result.unmapped.map((row) => row.connector_id)).toEqual(['unknown'])
  })
})
