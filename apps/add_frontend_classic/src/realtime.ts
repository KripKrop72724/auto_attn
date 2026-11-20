import { useEffect, useRef, useState } from 'react'

export type RealtimeState = 'connecting' | 'live' | 'reconnecting' | 'stale'
export type RealtimeTopic =
  | 'attendance'
  | 'alert'
  | 'users'
  | 'reconciliation'
  | 'command'
  | 'log'
  | 'identity'
  | 'firmware'
  | 'provisioning'
  | 'device'
  | 'backend_error'
  | 'resync'

const serverEvents = [
  'attendance', 'alert', 'users', 'reconciliation', 'command', 'log', 'firmware', 'provisioning', 'device',
  'identity_snapshot', 'identity_conflict', 'historical_identity',
  'historical_event_group_identity', 'backend_error', 'resync',
  // Mixed-version deployment: recognize wire names until every ADD instance
  // publishes canonical topics.
  'heartbeat', 'attendance_batch', 'command_update', 'user_snapshot',
  'queue_evidence', 'oracle_receipt_batch', 'source_tail_chunk', 'source_probe_result',
  'reconcile_anchor', 'reconcile_chunk', 'reconcile_source_manifest', 'reconcile_assignment_release',
  'hikvision_profile_page', 'hikvision_history_page', 'hikvision_observation',
] as const

export const normalizeTopic = (name: string): RealtimeTopic => {
  if (name === 'heartbeat') return 'device'
  if (['attendance_batch', 'oracle_receipt_batch', 'hikvision_observation'].includes(name)) return 'attendance'
  if (name === 'command_update') return 'command'
  if (['user_snapshot', 'hikvision_profile_page'].includes(name)) return 'users'
  if (name.startsWith('reconcile_') || name.startsWith('source_') || ['queue_evidence', 'hikvision_history_page'].includes(name)) return 'reconciliation'
  if (name.startsWith('identity') || name.startsWith('historical')) return 'identity'
  if (name === 'backend_error') return 'backend_error'
  return name as RealtimeTopic
}

export function useRealtime(
  enabled: boolean,
  onTopics: (topics: ReadonlySet<RealtimeTopic>) => void,
) {
  const [state, setState] = useState<RealtimeState>('connecting')
  const [lastSyncAt, setLastSyncAt] = useState<Date | null>(null)
  const callbackRef = useRef(onTopics)
  callbackRef.current = onTopics

  useEffect(() => {
    if (!enabled) return
    let active = true
    let lastSuccess = Date.now()
    let lastAttempt = lastSuccess
    let flushTimer = 0
    let stream: EventSource | null = null
    const pending = new Set<RealtimeTopic>()

    const flush = () => {
      flushTimer = 0
      if (!active || !pending.size) return
      callbackRef.current(new Set(pending))
      pending.clear()
    }
    const enqueue = (topic: RealtimeTopic) => {
      pending.add(topic)
      if (!flushTimer) flushTimer = window.setTimeout(flush, 120)
    }
    const connect = () => {
      lastAttempt = Date.now()
      stream?.close()
      stream = null
      if (typeof EventSource === 'undefined') return
      try {
        const candidate = new EventSource('/events/v1/stream', { withCredentials: true })
        stream = candidate
        const markHealthy = (topic?: RealtimeTopic) => {
          // A closed stream can still have queued callbacks. Only the current
          // connection may update transport evidence or schedule a refresh.
          if (!active || stream !== candidate) return
          lastSuccess = Date.now()
          setLastSyncAt(new Date(lastSuccess))
          setState('live')
          if (topic) enqueue(topic)
        }
        candidate.onopen = () => markHealthy('resync')
        candidate.onmessage = () => markHealthy('resync')
        serverEvents.forEach((eventName) => {
          candidate.addEventListener(eventName, () => markHealthy(normalizeTopic(eventName)))
        })
        candidate.addEventListener('keepalive', () => markHealthy())
        candidate.onerror = () => {
          if (!active || stream !== candidate) return
          setState(Date.now() - lastSuccess >= 30_000 ? 'stale' : 'reconnecting')
        }
      } catch {
        // A browser that cannot construct EventSource must retain the polling
        // fallback. Retry on the same bounded schedule as a closed connection.
        setState('stale')
      }
    }
    const resync = () => {
      const now = Date.now()
      if (now - lastSuccess >= 30_000) setState('stale')
      enqueue('resync')
      // Native EventSource retries transient errors. Replace a permanently
      // CLOSED connection after 30 s, or a silent OPEN/CONNECTING one after
      // 60 s. Focus events cannot create a rapid reconnect loop.
      if (now - lastAttempt < 30_000) return
      if (!stream || stream.readyState === EventSource.CLOSED || now - Math.max(lastSuccess, lastAttempt) >= 60_000) connect()
    }
    setState('connecting')
    setLastSyncAt(null)
    connect()
    const onVisible = () => { if (document.visibilityState === 'visible') resync() }
    window.addEventListener('focus', resync)
    document.addEventListener('visibilitychange', onVisible)
    const staleTimer = window.setInterval(() => {
      // OPEN alone cannot prove a functioning stream. Poll even when OPEN;
      // this also works with old servers that send invisible comment pings.
      resync()
    }, 30_000)

    return () => {
      active = false
      stream?.close()
      window.removeEventListener('focus', resync)
      document.removeEventListener('visibilitychange', onVisible)
      window.clearInterval(staleTimer)
      window.clearTimeout(flushTimer)
    }
  }, [enabled])

  return { state, lastSyncAt }
}
