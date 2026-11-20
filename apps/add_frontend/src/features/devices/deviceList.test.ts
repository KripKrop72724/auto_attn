import { describe, expect, it } from 'vitest'
import { device } from '../../test/renderApp'
import type { Device } from '../../types'
import {
  DEFAULT_FILTERS, applyFilters, cityOf, cityOptions, deviceViews, fleetCounts, notWorking, softwareVersion, sortViews,
  spareCount, statusCounts, versionOptions,
} from './deviceList'

const row = (id: string, code: string, extra: Partial<Device> = {}): Device => ({
  ...device,
  connector_id: id,
  display_name: code,
  zone_id: code,
  zone_name: code,
  friendly_name: undefined,
  division: undefined,
  ...extra,
})

const fleet: Device[] = [
  row('tower-13', 'ZONE-SLICTOWER-13FL', { last_seen_at: '2026-11-20T08:00:00Z', zkt: { ...device.zkt!, serial: 'ISB-0013' } }),
  row('tower-3', 'ZONE-SLICTOWER-3FL', { last_seen_at: '2026-11-20T07:00:00Z' }),
  row('bld9-3', 'LF-ZONE-BLD9-03', { state: 'DEGRADED', health: { mode: 'ENFORCED', tier: 'DEGRADED', primary: { code: 'ESP_DURABILITY_FAULT', since: '2026-11-20T04:24:00Z' } } as Device['health'] }),
  row('bld9-1', 'LF-ZONE-BLD9-01'),
  row('multan-2', 'ZONE-MULTAN-02', { state: 'OFFLINE', connected: false, firmware_version: null, terminal_vendor: 'hikvision', zkt: null }),
  row('swat', 'ZONE-SWAT-01', { firmware_version: 'zone-lite-2.8.2' }),
  row('spare', 'LF-ZONE-BLD1-01', { is_spare: true, state: 'OFFLINE', connected: false }),
]

describe('device rows for Home and Devices', () => {
  const views = deviceViews(fleet)
  const byId = (id: string) => views.find((view) => view.id === id)!

  it('names devices with the /status rule when the backend sends no friendly name', () => {
    expect(byId('tower-3').name).toBe('SLIC Tower · 3rd Floor')
    expect(byId('bld9-3').name).toBe('Building 9 · Device 3')
    expect(byId('swat').name).toBe('Swat')
    expect(byId('spare').name).toBe('Building 1 · Spare')
    expect(byId('bld9-3').division).toBe('life')
    expect(byId('swat').division).toBe('health')
  })

  it('prefers the backend fields', () => {
    const [view] = deviceViews([row('x', 'ZONE-X-01', { friendly_name: 'Lahore · Device 1', division: 'life', city: 'Lahore' } as Partial<Device>)])
    expect(view).toMatchObject({ name: 'Lahore · Device 1', division: 'life', city: 'Lahore' })
  })

  it('reads cities and software versions', () => {
    expect(cityOf('Building 9')).toBe('Karachi')
    expect(cityOf('Group & Pension Building 8')).toBe('Karachi')
    expect(cityOf('SLIC Tower')).toBe('Islamabad')
    expect(cityOf('Multan')).toBe('Multan')
    expect(softwareVersion('zone-lite-2.8.2')).toBe('2.8.2')
    expect(softwareVersion(null)).toBeNull()
  })

  it('sorts needs attention first, then by name with numbers in order, and never by last contact', () => {
    const sorted = sortViews(views.filter((view) => !view.spare), 'attention').map((view) => view.id)
    expect(sorted).toEqual(['multan-2', 'bld9-3', 'bld9-1', 'tower-3', 'tower-13', 'swat'])
    expect(sortViews(views, 'name').map((view) => view.name).indexOf('SLIC Tower · 3rd Floor'))
      .toBeLessThan(sortViews(views, 'name').map((view) => view.name).indexOf('SLIC Tower · 13th Floor'))
    // A heartbeat that only moves last_seen_at keeps the order.
    const later = deviceViews(fleet.map((item) => ({ ...item, last_seen_at: new Date().toISOString() })))
    expect(sortViews(later.filter((view) => !view.spare), 'attention').map((view) => view.id)).toEqual(sorted)
  })

  it('filters by status, city, brand, version, spare and search, with chip counts that follow the other filters', () => {
    const filters = { ...DEFAULT_FILTERS }
    expect(statusCounts(views, filters)).toEqual({ all: 6, attention: 1, offline: 1, working: 4, setup: 0 })
    expect(spareCount(views, filters)).toBe(1)
    expect(applyFilters(views, { ...filters, status: 'offline' }).map((view) => view.id)).toEqual(['multan-2'])
    expect(applyFilters(views, { ...filters, cities: ['Karachi'] }).map((view) => view.id).sort()).toEqual(['bld9-1', 'bld9-3'])
    expect(statusCounts(views, { ...filters, cities: ['Karachi'] })).toMatchObject({ all: 2, attention: 1, offline: 0 })
    expect(applyFilters(views, { ...filters, brand: 'hikvision' }).map((view) => view.id)).toEqual(['multan-2'])
    expect(applyFilters(views, { ...filters, version: '2.8.2' }).map((view) => view.id)).toEqual(['swat'])
    expect(applyFilters(views, { ...filters, version: 'unknown' }).map((view) => view.id)).toEqual(['multan-2'])
    expect(applyFilters(views, { ...filters, spare: true }).map((view) => view.id)).toEqual(['spare'])
    expect(applyFilters(views, { ...filters, q: 'isb-0013' }).map((view) => view.id)).toEqual(['tower-13'])
    expect(applyFilters(views, { ...filters, q: 'life' }).map((view) => view.id).sort()).toEqual(['bld9-1', 'bld9-3'])
    expect(applyFilters(views, { ...filters, q: 'karachi' })).toHaveLength(2)
    expect(applyFilters(views, { ...filters, q: 'ZONE-SWAT' }).map((view) => view.id)).toEqual(['swat'])
    expect(applyFilters(views, { ...filters, division: 'life' }).map((view) => view.id).sort()).toEqual(['bld9-1', 'bld9-3'])
    expect(cityOptions(views, filters).map((option) => option.city)).toEqual(['Islamabad', 'Karachi', 'Multan', 'Swat'])
    expect(versionOptions(views, filters).map((option) => option.id)).toEqual(['2.8.2', '2.1.0', 'unknown'])
  })

  it('counts Home tiles from the same rows, spares apart', () => {
    expect(fleetCounts(views)).toMatchObject({ total: 6, working: 4, attention: 1, offline: 1, spares: 1 })
    expect(notWorking(views).map((view) => view.id)).toEqual(['multan-2', 'bld9-3'])
  })
})
