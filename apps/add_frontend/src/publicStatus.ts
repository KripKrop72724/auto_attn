// The public /status page: Online or Offline per device, and since when. The
// backend (zk_add/public_status.py) decides the status and the friendly names.

export type PublicZoneKey = 'life' | 'health'

export interface PublicDevice {
  name: string
  location: string
  unit: string | null
  zone: PublicZoneKey
  code: string
  status: 'online' | 'offline'
  since: string
}

export interface PublicZone {
  zone: PublicZoneKey
  name: string
  total: number
  online: number
  offline: number
}

export interface PublicStatusSnapshot {
  updated_at: string
  total: number
  online: number
  offline: number
  zones: PublicZone[]
  devices: PublicDevice[]
}

export const PUBLIC_STATUS_PATH = '/api/public/v1/status'
export const PUBLIC_STATUS_STREAM_PATH = '/events/public/v1/status'
const TIME_ZONE = 'Asia/Karachi'

export const isPublicStatusPath = (pathname: string) => /^\/status\/?$/.test(pathname)

const dayKey = new Intl.DateTimeFormat('en-CA', { timeZone: TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit' })
const clock = new Intl.DateTimeFormat('en-US', { timeZone: TIME_ZONE, hour: 'numeric', minute: '2-digit' })
const dayMonth = new Intl.DateTimeFormat('en-GB', { timeZone: TIME_ZONE, day: 'numeric', month: 'short' })
const dayMonthYear = new Intl.DateTimeFormat('en-GB', { timeZone: TIME_ZONE, day: 'numeric', month: 'short', year: 'numeric' })

// "4:10 PM", "yesterday, 4:10 PM", "21 Oct, 4:10 PM" or "21 Oct 2025, 4:10 PM", in Pakistan time.
export function formatSince(value: string, now: Date) {
  const date = new Date(value)
  if (Number.isNaN(+date)) return ''
  const time = clock.format(date)
  const day = dayKey.format(date)
  if (day === dayKey.format(now)) return time
  if (day === dayKey.format(new Date(+now - 86_400_000))) return `yesterday, ${time}`
  const sameYear = day.slice(0, 4) === dayKey.format(now).slice(0, 4)
  return `${(sameYear ? dayMonth : dayMonthYear).format(date)}, ${time}`
}

// "less than a minute", "12 min", "3 h 20 min", "1 day 4 h", "12 days".
export function formatDuration(value: string, now: Date) {
  const minutes = Math.max(0, Math.floor((+now - +new Date(value)) / 60_000))
  if (!Number.isFinite(minutes)) return ''
  if (minutes < 1) return 'less than a minute'
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`
  const days = Math.floor(hours / 24)
  const plural = days === 1 ? 'day' : 'days'
  return days < 7 && hours % 24 ? `${days} ${plural} ${hours % 24} h` : `${days} ${plural}`
}

export const formatClock = (value: string) => {
  const date = new Date(value)
  return Number.isNaN(+date) ? '' : clock.format(date)
}

export const devicesInZone = (snapshot: PublicStatusSnapshot, zone: PublicZoneKey) =>
  snapshot.devices.filter((device) => device.zone === zone)
