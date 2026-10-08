import { describe, expect, it } from 'vitest'
import type { Device, FirmwareRelease } from '../types'
import { hilAllowedTargets, hilDevice, hilDeviceMismatch, hilScopeLabel } from './hilTargets'

const target = { connector_id: 'exact', mac: 'a4:cb:8f:d4:66:64', terminal_serial: 'PGB1261200074' }
const release = { state: 'HIL_ONLY', hil_targets: [target], hil_next_target: target } as FirmwareRelease
const device = { connector_id: target.connector_id, hardware_id: target.mac,
  display_name: 'same name', is_spare: false,
  zkt: { serial: target.terminal_serial, confirmed_serial: target.terminal_serial,
    expected_serial: target.terminal_serial, terminal_binding_state: 'CONFIRMED' } } as Device

describe('ordered HIL destination', () => {
  it('keeps deferred zones in the denominator without counting them as passed', () => {
    const scheduled = { ...release, hil_schedule: { policy: 'ZKT_CONNECTIVITY_DEFERRAL_V1', denominator: 17,
      counts: { PASSED: 1, PENDING: 12, DEFERRED_OFFLINE: 2, BLOCKED: 2 }, hold: null, rows: [] } }
    expect(hilScopeLabel(scheduled)).toBe('1/17 passed · 12 pending · 2 deferred offline · 2 blocked')
    expect(hilAllowedTargets(scheduled)).toEqual([target])
    expect(hilAllowedTargets({ ...scheduled, hil_next_target: null, hil_allowed_targets: [] })).toEqual([])
  })
  it('distinguishes three factory bridge trials from all seventeen nationwide targets', () => {
    const scheduled = { ...release, hil_schedule: { policy: 'ZKT_FACTORY_TRIAL_V1', denominator: 17,
      trial_target_count: 3,
      counts: { PASSED: 1, PENDING: 1, DEFERRED_OFFLINE: 1, BLOCKED: 0, NOT_APPLICABLE_TO_THIS_BRIDGE: 14 },
      hold: 'FACTORY_3FL_VERDICT_REQUIRED', rows: [] } }
    expect(hilScopeLabel(scheduled)).toBe('1/3 factory bridge trials passed · 17 nationwide targets · 14 require another bridge · 1 pending · 1 deferred offline · 0 blocked · progression held')
    expect(hilAllowedTargets({ ...scheduled, hil_next_target: null, hil_allowed_targets: [] })).toEqual([])
  })
  it('uses the server-selected exact target, excluding the same-name spare', () => {
    const spare = { ...device, connector_id: 'spare', is_spare: true }
    expect(hilDevice(release, [spare, device])).toBe(device)
    expect(hilDevice(release, [spare])).toBeNull()
  })
  it('holds selection without next-target acceptance evidence', () => {
    const held = { ...release, hil_next_target: null, hil_scope_message: 'Awaiting acceptance' }
    expect(hilDevice(held, [device])).toBeNull()
    expect(hilScopeLabel(held)).toBe('Awaiting acceptance')
  })
  it('rejects a replacement terminal and mismatched connector', () => {
    expect(hilDevice(release, [{ ...device, connector_id: 'other' }])).toBeNull()
    expect(hilDevice(release, [{ ...device, zkt: { ...device.zkt!, serial: 'replacement' } }])).toBeNull()
    expect(hilDeviceMismatch(release, [{ ...device, zkt: { ...device.zkt!, confirmed_serial: null } }]))
      .toMatch(/Confirmed terminal serial/)
    expect(hilDeviceMismatch(release, [{ ...device, zkt: { ...device.zkt!, expected_serial: null } }]))
      .toMatch(/Expected terminal serial/)
    expect(hilDeviceMismatch(release, [device])).toBeNull()
    const pending = { ...device, zkt: { ...device.zkt!, terminal_binding_state: 'PENDING_DEVICE_ACK' } }
    expect(hilDevice(release, [pending])).toBeNull()
    expect(hilDeviceMismatch(release, [pending])).toMatch(/wait for device acknowledgement/)
  })
  it('preserves legacy single-target support', () => {
    expect(hilDevice({ state: 'HIL_ONLY', hil_target_mac: target.mac } as FirmwareRelease, [device])).toBe(device)
  })
  it('selects an independently allowed HIL target by zone without admitting another zone', () => {
    const swat = { connector_id: 'swat', mac: 'ac:27:6e:a5:47:64', terminal_serial: 'AEXH232260005' }
    const peshawar = { connector_id: 'peshawar', mac: 'e0:72:a1:d7:05:c4', terminal_serial: 'CJH9211060009' }
    const swatDevice = { ...device, connector_id: swat.connector_id, hardware_id: swat.mac,
      zone_id: 'SWAT', zkt: { ...device.zkt!, serial: swat.terminal_serial,
        expected_serial: swat.terminal_serial, confirmed_serial: swat.terminal_serial } } as Device
    const slicDevice = { ...device, zone_id: 'SLICTOWER-3FL' } as Device
    const gated = { ...release, hil_targets: [swat, target, peshawar],
      hil_allowed_targets: [swat, target] } as FirmwareRelease
    expect(hilAllowedTargets(gated)).toEqual([swat, target])
    expect(hilDevice(gated, [swatDevice, slicDevice], 'SLICTOWER-3FL')).toBe(slicDevice)
    expect(hilDevice(gated, [swatDevice, slicDevice], 'PESHAWAR')).toBeNull()
    expect(hilDeviceMismatch(gated, [swatDevice, slicDevice], 'PESHAWAR')).toMatch(/outside/)
    expect(hilScopeLabel(gated)).toMatch(/2 exact HIL targets/)
  })
})
