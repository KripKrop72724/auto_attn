import type { Device, FirmwareRelease } from '../types'

export function hilDevice(release: FirmwareRelease | null | undefined, devices: Device[]): Device | null {
  if (!release || release.state !== 'HIL_ONLY') return null
  if (release.hil_targets) {
    const target = release.hil_next_target
    if (!target) return null
    return devices.find(device => !device.is_spare &&
      device.connector_id === target.connector_id &&
      device.hardware_id.toLowerCase() === target.mac.toLowerCase() &&
      device.zkt?.serial === target.terminal_serial &&
      device.zkt?.expected_serial === target.terminal_serial &&
      device.zkt?.confirmed_serial === target.terminal_serial) || null
  }
  return devices.find(device => !device.is_spare && release.hil_target_mac &&
    device.hardware_id.toLowerCase() === release.hil_target_mac.toLowerCase()) || null
}

export function hilScopeLabel(release: FirmwareRelease): string {
  if (release.hil_targets) {
    const target = release.hil_next_target
    return target
      ? `${release.hil_targets.length} ordered targets · next ${target.mac} · ${target.terminal_serial}`
      : release.hil_scope_message || 'Ordered HIL targets are on hold'
  }
  return release.hil_target_mac || 'Target MAC is unavailable'
}
