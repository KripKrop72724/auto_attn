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
    let lastSuccess = Date.now()
    let flushTimer = 0
    let staleTimer = 0
    const pending = new Set<RealtimeTopic>()
    const stream = typeof EventSource === 'undefined' ? null : new EventSource('/events/v1/stream', { withCredentials: true })

    const flush = () => {
      flushTimer = 0
      if (!pending.size) return
      callbackRef.current(new Set(pending))
      pending.clear()
    }
    const enqueue = (topic: RealtimeTopic) => {
      pending.add(topic)
      if (!flushTimer) flushTimer = window.setTimeout(flush, 120)
    }
    const markHealthy = () => {
      lastSuccess = Date.now()
      setLastSyncAt(new Date(lastSuccess))
      setState('live')
    }

    setState('connecting')
    if (stream) stream.onopen = () => {
      markHealthy()
      enqueue('resync')
    }
    if (stream) stream.onmessage = () => {
      markHealthy()
      enqueue('resync')
    }
    serverEvents.forEach((eventName) => {
      stream?.addEventListener(eventName, () => {
        markHealthy()
        enqueue(normalizeTopic(eventName))
      })
    })
    stream?.addEventListener('keepalive', markHealthy)
    if (stream) stream.onerror = () => setState(Date.now() - lastSuccess > 30_000 ? 'stale' : 'reconnecting')
    const resync = () => enqueue('resync')
    const onVisible = () => { if (document.visibilityState === 'visible') resync() }
    window.addEventListener('focus', resync)
    document.addEventListener('visibilitychange', onVisible)
    staleTimer = window.setInterval(() => {
      // OPEN alone cannot prove a functioning stream. Poll even when OPEN;
      // this also works with old servers that send invisible comment pings.
      if (Date.now() - lastSuccess >= 30_000) setState('stale')
      enqueue('resync')
    }, 30_000)

    return () => {
      stream?.close()
      window.removeEventListener('focus', resync)
      document.removeEventListener('visibilitychange', onVisible)
      window.clearInterval(staleTimer)
      window.clearTimeout(flushTimer)
    }
  }, [enabled])

  return { state, lastSyncAt }
}
