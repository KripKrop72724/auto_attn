import { describe, expect, it, vi } from 'vitest'
import { DeviceSnapshots, isFullDeviceRow, mergeDevice } from './deviceData'
import type { Device, ZktDevice } from './types'

const zkt = {
  id: 1, serial: 'ADZV211860253', model: 'MB20/ID', online: true, certification_observations: 3,
  capabilities: { user_write: true, create_user: true, protocol_restart: true, hikvision_health: 'x' },
} as unknown as ZktDevice

const detail = {
  connector_id: 'connector-one', display_name: 'ZONE-SWAT-01', zone_id: 'ZONE-SWAT-01', state: 'ONLINE',
  snapshot_at: '2026-11-06T05:00:00Z', zkt, firmware_diagnostics: { storage: { ok: true } }, firmware_diagnostics_at: '2026-11-06T05:00:00Z',
  health: { mode: 'ENFORCED', tier: 'ONLINE' },
} as unknown as Device

describe('device rows', () => {
  it('keeps the detail fields when a slim list row arrives later', () => {
    const slim = {
      ...detail, payload: 'list', snapshot_at: '2026-11-06T05:01:00Z', state: 'OFFLINE', firmware_diagnostics: null,
      zkt: { ...zkt, online: false, capabilities: { user_write: false }, certification_observations: undefined },
    } as unknown as Device
    const merged = mergeDevice(detail, slim)
    expect(merged.state).toBe('OFFLINE')
    expect(merged.zkt?.online).toBe(false)
    expect(merged.zkt?.capabilities).toEqual({ user_write: false, create_user: true, protocol_restart: true, hikvision_health: 'x' })
    expect(merged.zkt?.certification_observations).toBe(3)
    expect(merged.firmware_diagnostics).toEqual({ storage: { ok: true } })
    expect(merged.payload).toBeUndefined()
  })

  it('lets a full row replace capabilities (a capability can go away)', () => {
    const full = { ...detail, snapshot_at: '2026-11-06T05:02:00Z', zkt: { ...zkt, capabilities: { user_write: true } } } as Device
    expect(mergeDevice(detail, full).zkt?.capabilities).toEqual({ user_write: true })
    const unbound = { ...detail, snapshot_at: '2026-11-06T05:03:00Z', zkt: null } as Device
    expect(mergeDevice(detail, unbound).zkt).toBeNull()
  })

  it('knows "not loaded yet" apart from "no devices", and ignores broken rows', () => {
    const store = new DeviceSnapshots()
    const listener = vi.fn()
    store.subscribe(listener)
    expect(store.loaded()).toBe(false)
    store.replaceFleet([])
    expect(store.loaded()).toBe(true)
    expect(store.all()).toEqual([])
    store.put([null as unknown as Device, { foo: 1 } as unknown as Device])
    expect(store.all()).toEqual([])
    store.clear()
    expect(store.loaded()).toBe(false)
  })

  it('applies a newer live row in place and never an older one', () => {
    const store = new DeviceSnapshots()
    store.replaceFleet([detail])
    store.put([{ ...detail, payload: 'list', state: 'DEGRADED', snapshot_at: '2026-11-06T05:05:00Z', zkt: { ...zkt, capabilities: {} } } as Device])
    expect(store.get('connector-one')?.state).toBe('DEGRADED')
    expect(store.get('connector-one')?.zkt?.capabilities).toMatchObject({ protocol_restart: true })
    store.put([{ ...detail, state: 'ONLINE', snapshot_at: '2026-11-06T04:00:00Z' }])
    expect(store.get('connector-one')?.state).toBe('DEGRADED')
  })

  it.each([
    ['a full row with health', detail, true],
    ['a row without health', { ...detail, health: null }, false],
    ['a spare change', { connector_id: 'connector-one', is_spare: true }, false],
    ['a COMM Key state', { connector_id: 'connector-one', comm_key_state: 'SET' }, false],
    ['a machine binding state', { connector_id: 'connector-one', terminal_binding_state: 'BOUND' }, false],
    ['a connection change', { connector_id: 'connector-one', connected: false }, false],
    ['no data', null, false],
  ])('treats %s as %s for an in-place update', (_name, data, full) => {
    expect(isFullDeviceRow(data)).toBe(full)
  })
})
