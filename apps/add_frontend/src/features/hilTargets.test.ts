import { describe, expect, it } from 'vitest'
import type { Device, FirmwareRelease } from '../types'
import { hilDevice, hilDeviceMismatch, hilScopeLabel } from './hilTargets'

const target = { connector_id: 'exact', mac: 'a4:cb:8f:d4:66:64', terminal_serial: 'PGB1261200074' }
const release = { state: 'HIL_ONLY', hil_targets: [target], hil_next_target: target } as FirmwareRelease
const device = { connector_id: target.connector_id, hardware_id: target.mac,
  display_name: 'same name', is_spare: false,
  zkt: { serial: target.terminal_serial, confirmed_serial: target.terminal_serial,
    expected_serial: target.terminal_serial, terminal_binding_state: 'CONFIRMED' } } as Device

describe('ordered HIL destination', () => {
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
})
