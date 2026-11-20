export type StatusPattern = 'confirmed' | 'waiting' | 'blocked' | 'notice'

export const normalizedStatus = (state: unknown) =>
  typeof state === 'string' && state.trim() ? state.trim() : 'UNKNOWN'

const acronyms = new Set([
  'ADD', 'API', 'CNIC', 'ESP', 'ESP32', 'ETA', 'HIL', 'HR', 'HTTP', 'ID', 'IP', 'MAC', 'NVS', 'OK', 'ORDS', 'OS',
  'OTA', 'PKT', 'RSSI', 'SHA', 'TLS', 'UID', 'USB', 'UTC', 'ZKT',
])
const properNouns: Record<string, string> = { ORACLE: 'Oracle', HIKVISION: 'Hikvision', WIFI: 'Wi-Fi' }

// Machine states arrive as SCREAMING_SNAKE_CASE; people read sentence case.
export function humanizeStatus(state: unknown) {
  const value = normalizedStatus(state).replaceAll('_', ' ').replace(/\s+/g, ' ')
  if (/[a-z]/.test(value)) return value
  return value.split(' ').map((word, index) => {
    if (acronyms.has(word) || /\d/.test(word)) return word
    if (properNouns[word]) return properNouns[word]
    const lower = word.toLowerCase()
    return index === 0 ? lower.charAt(0).toUpperCase() + lower.slice(1) : lower
  }).join(' ')
}

export const statusPattern = (state: unknown): StatusPattern => {
  const normalized = normalizedStatus(state).toUpperCase()
  // Checked before the substring rules: WARNING would otherwise render this
  // online tier with the same amber pattern as DEGRADED.
  if (normalized === 'ONLINE_WITH_WARNINGS') return 'notice'
  if (
    [
      'ONLINE', 'SUCCEEDED', 'CERTIFIED', 'ACTIVE', 'OK', 'RESOLVED', 'COMPLETE', 'COMPLETED', 'AVAILABLE', 'ACKNOWLEDGED',
      'READY', 'ENABLED', 'CONNECTED', 'CONFIRMED', 'VERIFIED', 'VERIFIED_ONLINE', 'HEALTHY', 'SUPPORTED', 'PASSED', 'RELEASED',
    ].includes(normalized) ||
    normalized.includes('ACKED')
  )
    return 'confirmed'
  if (
    ['OFFLINE', 'FAILED', 'PARTIAL', 'CRITICAL', 'HIGH', 'EXPIRED', 'INVALIDATED', 'QUARANTINED', 'BLOCKED_IDENTITY', 'CNIC_NOT_LINKED', 'REVOKED'].some(
      (item) => normalized.includes(item),
    )
  )
    return 'blocked'
  if (
    ['WAITING', 'RETRYING', 'DEGRADED', 'FLAPPING', 'PENDING', 'RUNNING', 'WARNING', 'PAUSED', 'CANCEL_REQUESTED'].some((item) =>
      normalized.includes(item),
    )
  )
    return 'waiting'
  return 'notice'
}

// ONLINE_WITH_WARNINGS is an online tier: the ESP delivers attendance, but a
// warning needs review. Fleet availability counts both.
export const isOnlineState = (state: unknown) =>
  ['ONLINE', 'ONLINE_WITH_WARNINGS'].includes(normalizedStatus(state).toUpperCase())

const terminalLinkPatterns: Record<string, StatusPattern> = {
  CONNECTED: 'confirmed',
  STABILIZING: 'notice',
  MAINTENANCE: 'notice',
  STARTING: 'notice',
  UNKNOWN: 'notice',
  RECONNECTING: 'waiting',
  FLAPPING: 'waiting',
  DISCONNECTED: 'blocked',
  ERROR: 'blocked',
}

// The terminal link is reported separately from the ESP lifecycle tier.
export const terminalLinkPattern = (state: unknown): StatusPattern =>
  terminalLinkPatterns[normalizedStatus(state).toUpperCase()] || 'notice'

export const terminalLinkNeedsAttention = (state: unknown) =>
  ['DISCONNECTED', 'FLAPPING', 'ERROR'].includes(normalizedStatus(state).toUpperCase())

// Acknowledging annotates an alert that stays OPEN, so alert rows cannot use the
// global ACKNOWLEDGED -> confirmed mapping kept for other workflow statuses.
export const alertStatePattern = (alert: { state?: unknown; severity?: unknown; acknowledged_at?: string | null }): StatusPattern => {
  const state = normalizedStatus(alert.state).toUpperCase()
  if (state === 'RESOLVED') return 'confirmed'
  if (state === 'ACKNOWLEDGED' || alert.acknowledged_at) return 'notice'
  return statusPattern(alert.severity)
}

export const firmwareLabel = (version?: string | null) =>
  version ? version.replace(/^zone-lite-/i, 'Zone Lite ') : 'Unknown'
