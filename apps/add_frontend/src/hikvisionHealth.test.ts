import { describe, expect, it } from 'vitest'
import { deviceActivity } from './hikvisionHealth'
import type { Device } from './types'
const device = { firmware_family: 'hikvision', connected: true, current_activity: 'HIKVISION_POLL_2S', hikvision: { poll_error: 2, last_successful_poll_epoch: 0 } } as Device

describe('Hikvision transport and capture status', () => {
  it('shows terminal failure even while ESP heartbeats are connected', () => {
    expect(deviceActivity(device)).toContain('Terminal unreachable')
    expect(deviceActivity(device)).not.toContain('POLL_2S')
  })
  it('does not label an untested terminal healthy from a zero error', () => {
    expect(deviceActivity({ ...device, hikvision: { ...device.hikvision!, poll_error: 0 } })).toContain('Waiting for terminal check')
  })
  it('distinguishes ESP disconnection and storage errors', () => {
    expect(deviceActivity({ ...device, connected: false })).toBe('ESP disconnected from ADD')
    expect(deviceActivity({ ...device, last_error_message: 'Local persistence failed' })).toBe('Local persistence failed')
  })
  it('leaves ZKT activity intact', () => {
    expect(deviceActivity({ ...device, firmware_family: 'zkt', current_activity: 'Live capture' })).toBe('Live capture')
  })
})
