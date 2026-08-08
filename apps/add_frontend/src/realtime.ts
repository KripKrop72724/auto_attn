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
  | 'device'
  | 'backend_error'
  | 'resync'

const serverEvents = [
  'attendance', 'alert', 'users', 'reconciliation', 'command', 'log', 'firmware', 'device',
  'identity_snapshot', 'identity_conflict', 'historical_identity',
  'historical_event_group_identity', 'backend_error',
] as const

const normalizeTopic = (name: string): RealtimeTopic => {
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
    if (!enabled || typeof EventSource === 'undefined') return
    let lastSuccess = Date.now()
    let flushTimer = 0
    let staleTimer = 0
    const pending = new Set<RealtimeTopic>()
    const stream = new EventSource('/events/v1/stream', { withCredentials: true })

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
    stream.onopen = () => {
      markHealthy()
      enqueue('resync')
    }
    stream.onmessage = () => {
      markHealthy()
      enqueue('resync')
    }
    serverEvents.forEach((eventName) => {
      stream.addEventListener(eventName, () => {
        markHealthy()
        enqueue(normalizeTopic(eventName))
      })
    })
    stream.onerror = () => setState(Date.now() - lastSuccess > 30_000 ? 'stale' : 'reconnecting')
    staleTimer = window.setInterval(() => {
      if (stream.readyState !== EventSource.OPEN) {
        setState(Date.now() - lastSuccess > 30_000 ? 'stale' : 'reconnecting')
        if (Date.now() - lastSuccess > 30_000) enqueue('resync')
      }
    }, 30_000)

    return () => {
      stream.close()
      window.clearInterval(staleTimer)
      window.clearTimeout(flushTimer)
    }
  }, [enabled])

  return { state, lastSyncAt }
}
