import type { Device, FirmwareRelease } from '../types'

export function hilAllowedTargets(release: FirmwareRelease | null | undefined) {
  if (!release || release.state !== 'HIL_ONLY' || !release.hil_targets) return []
  return release.hil_allowed_targets || (release.hil_next_target ? [release.hil_next_target] : [])
}

function selectedHilTarget(release: FirmwareRelease | null | undefined, devices: Device[], zoneId?: string) {
  const targets = hilAllowedTargets(release)
  return targets.find(target => {
    const device = devices.find(row => row.connector_id === target.connector_id)
    return device && (!zoneId || device.zone_id === zoneId)
  }) || (zoneId ? null : targets[0] || null)
}

export function hilDevice(release: FirmwareRelease | null | undefined, devices: Device[], zoneId?: string): Device | null {
  if (!release || release.state !== 'HIL_ONLY') return null
  if (release.hil_targets) {
    const target = selectedHilTarget(release, devices, zoneId)
    if (!target) return null
    return devices.find(device => !device.is_spare &&
      device.connector_id === target.connector_id &&
      device.hardware_id.toLowerCase() === target.mac.toLowerCase() &&
      device.zkt?.serial === target.terminal_serial &&
      device.zkt?.expected_serial === target.terminal_serial &&
      device.zkt?.confirmed_serial === target.terminal_serial &&
      device.zkt?.terminal_binding_state === 'CONFIRMED') || null
  }
  return devices.find(device => !device.is_spare && release.hil_target_mac &&
    device.hardware_id.toLowerCase() === release.hil_target_mac.toLowerCase()) || null
}

export function hilDeviceMismatch(release: FirmwareRelease | null | undefined, devices: Device[], zoneId?: string): string | null {
  if (release?.state !== 'HIL_ONLY' || !release.hil_targets || !hilAllowedTargets(release).length) return null
  const target = selectedHilTarget(release, devices, zoneId)
  if (!target) return 'This zone is outside the current HIL scope.'
  const device = devices.find(row => row.connector_id === target.connector_id)
  if (!device) return 'Target connector is missing from the active fleet.'
  if (device.is_spare) return 'Target connector is in spare inventory.'
  if (device.hardware_id.toLowerCase() !== target.mac.toLowerCase()) return 'Registered ESP MAC differs from the signed target.'
  if (!device.zkt) return 'No terminal is registered to the target connector.'
  if (device.zkt.serial !== target.terminal_serial) return `Observed terminal serial is ${device.zkt.serial || 'missing'}.`
  const binding = device.zkt.terminal_binding_state || 'UNKNOWN'
  if (device.zkt.expected_serial !== target.terminal_serial) {
    return `Expected terminal serial is ${device.zkt.expected_serial || 'missing'}; binding state ${binding}.`
  }
  if (device.zkt.confirmed_serial !== target.terminal_serial) {
    return `Confirmed terminal serial is ${device.zkt.confirmed_serial || 'missing'}; binding state ${binding}.`
  }
  if (binding !== 'CONFIRMED') return `Terminal binding is ${binding}; wait for device acknowledgement.`
  return null
}

export function hilScopeLabel(release: FirmwareRelease): string {
  if (release.hil_schedule) {
    const { counts, denominator, hold, trial_target_count: trialTargetCount } = release.hil_schedule
    const otherBridges = counts.NOT_APPLICABLE_TO_THIS_BRIDGE === undefined
      ? 'other targets require another bridge'
      : `${counts.NOT_APPLICABLE_TO_THIS_BRIDGE} require another bridge`
    const scope = trialTargetCount === undefined
      ? `${counts.PASSED}/${denominator} passed`
      : `${counts.PASSED}/${trialTargetCount} factory bridge trials passed · ${denominator} nationwide targets · ${otherBridges}`
    return `${scope} · ${counts.PENDING} pending · ${counts.DEFERRED_OFFLINE} deferred offline · ${counts.BLOCKED} blocked${hold ? ' · progression held' : ''}`
  }
  if (release.hil_targets) {
    const allowed = hilAllowedTargets(release)
    if (allowed.length > 1) return `${allowed.length} exact HIL targets open for independent trials`
    const target = allowed[0]
    return target
      ? `${release.hil_targets.length} ordered targets · next ${target.mac} · ${target.terminal_serial}`
      : release.hil_scope_message || 'Ordered HIL targets are on hold'
  }
  return release.hil_target_mac || 'Target MAC is unavailable'
}
