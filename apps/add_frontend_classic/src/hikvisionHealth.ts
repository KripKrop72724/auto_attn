import { humanizeStatus } from './status'
import type { Device } from './types'

export function deviceActivity(device: Device): string {
  if (device.firmware_family !== 'hikvision') return device.current_activity ? humanizeStatus(device.current_activity) : 'Idle'
  if (!device.connected) return 'ESP disconnected from ADD'
  if (device.last_error_message) return device.last_error_message
  const error = device.hikvision?.poll_error
  const reasons: Record<number, string> = {
    1: 'Terminal configuration invalid', 2: 'Terminal unreachable or request timed out',
    3: 'Terminal authentication rejected', 4: 'Terminal HTTP error',
    5: 'Terminal response too large', 6: 'Terminal identity or history changed',
    7: 'Invalid terminal response', 8: 'Local attendance storage failed',
  }
  if (error) return `ESP connected · ${reasons[error] || 'Terminal check failed'}`
  if (!device.hikvision?.last_successful_poll_epoch) return 'ESP connected · Waiting for terminal check'
  return 'ESP connected · Terminal capture healthy'
}
