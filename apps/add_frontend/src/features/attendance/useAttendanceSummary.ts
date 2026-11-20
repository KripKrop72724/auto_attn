// The day's real totals from the server (`/api/v1/attendance/summary`, ATT-8):
// refetched at most every 15 s on live check-ins and every 60 s when idle,
// never while the list is frozen. Ranges over 31 days are not asked for.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import { useThrottledRunner } from '../../lib/throttle'
import type { AttendanceSummary, GroupTotals } from './types'

export const SUMMARY_REFRESH_MS = 15_000
export const SUMMARY_IDLE_MS = 60_000

const num = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value) : 0)
const totals = (raw: unknown): GroupTotals => {
  const value = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  return {
    received: num(value.received), sent: num(value.sent), waiting: num(value.waiting), trying_again: num(value.trying_again),
    not_matched: num(value.not_matched), set_aside: num(value.set_aside), problem: num(value.problem),
    oldest_waiting_at: typeof value.oldest_waiting_at === 'string' ? value.oldest_waiting_at : null,
  }
}

/** The answer, or null when it is not a summary at all (never shown as zeros). */
export function normalizeSummary(raw: unknown): AttendanceSummary | null {
  const value = (raw && typeof raw === 'object' ? raw : null) as Record<string, unknown> | null
  if (!value || !value.totals || typeof value.totals !== 'object') return null
  const byDivision = (value.by_division && typeof value.by_division === 'object' ? value.by_division : {}) as Record<string, unknown>
  return {
    from_time: String(value.from_time || ''),
    to_time: typeof value.to_time === 'string' ? value.to_time : null,
    division: value.division === 'life' || value.division === 'health' ? value.division : null,
    computed_at: typeof value.computed_at === 'string' ? value.computed_at : new Date().toISOString(),
    max_age_seconds: num(value.max_age_seconds),
    totals: totals(value.totals),
    last_sent_at: typeof value.last_sent_at === 'string' ? value.last_sent_at : null,
    by_division: { life: totals(byDivision.life), health: totals(byDivision.health) },
  }
}

export type SummaryState = { data: AttendanceSummary | null; loading: boolean; failed: boolean; tooLong: boolean; retry: () => Promise<void> }

export function useAttendanceSummary({ query, tooLong, revision, frozen }: { query: string; tooLong: boolean; revision: number; frozen: boolean }): SummaryState {
  const [data, setData] = useState<AttendanceSummary | null>(null)
  const [loading, setLoading] = useState(!tooLong)
  const [failed, setFailed] = useState(false)
  const request = useRef<AbortController | null>(null)
  const queryRef = useRef(query)
  queryRef.current = query

  const load = useCallback(async () => {
    if (tooLong) return
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    try {
      const value = normalizeSummary(await api<unknown>(`/api/v1/attendance/summary?${queryRef.current}`, { signal: controller.signal }))
      if (controller.signal.aborted) return
      if (!value) throw new Error('not a summary')
      setData(value)
      setFailed(false)
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      if (!controller.signal.aborted) setFailed(true)
    } finally {
      if (request.current === controller) setLoading(false)
    }
  }, [tooLong])

  // A new window or division: forget the old figures (never show another day's numbers).
  useEffect(() => {
    if (tooLong) {
      request.current?.abort()
      setData(null)
      setLoading(false)
      setFailed(false)
      return
    }
    setData(null)
    setLoading(true)
    setFailed(false)
    void load()
  }, [load, query, tooLong])

  useEffect(() => () => request.current?.abort(), [])

  const runner = useThrottledRunner(load, SUMMARY_REFRESH_MS)
  const first = useRef(revision)
  useEffect(() => {
    if (revision === first.current) return
    first.current = revision
    if (!frozen && !tooLong) runner.schedule()
  }, [frozen, revision, runner, tooLong])

  useEffect(() => {
    if (frozen || tooLong) return
    const timer = window.setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      runner.schedule()
    }, SUMMARY_IDLE_MS)
    return () => window.clearInterval(timer)
  }, [frozen, runner, tooLong])

  const retry = useCallback(async () => {
    setFailed(false)
    await load()
  }, [load])

  return { data, loading, failed, tooLong, retry }
}
