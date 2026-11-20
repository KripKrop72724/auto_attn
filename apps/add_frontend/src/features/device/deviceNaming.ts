// Friendly name, place and division of a device: the backend fields first
// (HOME-1/HOME-2, be/device_names.py), the lib mirror only as a fallback.
import { deviceName } from '../../lib/deviceNames'
import type { Device } from '../../types'
import type { DeviceDetail } from '../../words/device'

export const deviceCode = (device: Pick<Device, 'display_name' | 'zone_id'>) => device.display_name || device.zone_id || ''

export function friendlyName(device: Pick<Device, 'display_name' | 'zone_id' | 'friendly_name'>): string {
  if (device.friendly_name?.trim()) return device.friendly_name.trim()
  const name = deviceName(deviceCode(device))
  return name.unit ? `${name.location} · ${name.unit}` : name.location || deviceCode(device)
}

export function devicePlace(device: DeviceDetail): string {
  if (device.place?.trim()) return device.place.trim()
  return deviceName(deviceCode(device)).location || friendlyName(device).split(' · ')[0]
}

export function deviceDivision(device: Pick<Device, 'display_name' | 'zone_id' | 'division'>): 'life' | 'health' {
  if (device.division === 'life' || device.division === 'health') return device.division
  return deviceName(deviceCode(device)).zone
}

export const isHikvision = (device: Pick<Device, 'firmware_family' | 'terminal_vendor'>) =>
  device.firmware_family === 'hikvision' || device.terminal_vendor === 'hikvision'
