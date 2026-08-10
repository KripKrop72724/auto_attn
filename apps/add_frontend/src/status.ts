export type StatusPattern = 'confirmed' | 'waiting' | 'blocked' | 'notice'

export const normalizedStatus = (state: unknown) =>
  typeof state === 'string' && state.trim() ? state.trim() : 'UNKNOWN'

export const statusPattern = (state: unknown): StatusPattern => {
  const normalized = normalizedStatus(state).toUpperCase()
  if (
    ['ONLINE', 'SUCCEEDED', 'CERTIFIED', 'ACTIVE', 'OK', 'RESOLVED', 'COMPLETE', 'COMPLETED', 'AVAILABLE', 'ACKNOWLEDGED'].includes(normalized) ||
    normalized.includes('ACKED')
  )
    return 'confirmed'
  if (
    ['OFFLINE', 'FAILED', 'PARTIAL', 'CRITICAL', 'HIGH', 'EXPIRED', 'INVALIDATED', 'QUARANTINED', 'BLOCKED_IDENTITY', 'REVOKED'].some(
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
