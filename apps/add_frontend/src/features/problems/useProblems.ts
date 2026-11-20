// Data for the Problems page (PRB-3). One request per filter change (the older
// one is aborted, so a late answer never overwrites newer filters); live
// changes re-ask the first page at most every 10 s and merge it by id, so rows
// loaded with "Show more" stay and the list never jumps back to the top.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, queryString } from '../../api'
import { useThrottledRunner } from '../../lib/throttle'
import type { AlertResolveResponse } from '../../types'
import type { ProblemApiParams, ProblemTab } from './problemFilters'
import type { ProblemCounts, ProblemRow, ProblemsResponse } from './types'

export const LIVE_REFRESH_MS = 10_000
const LEAVE_MS = 200
const FIXED_HOLD_MS = 1_000
const FIXED_NOW_MS = 4_000
const FRESH_MS = 1_200

/** 'fresh' = just arrived; 'fixed-now' = fixed by itself while watched; 'fixed-hold' = just marked fixed here; 'leaving' = collapsing out. */
export type RowPhase = 'fresh' | 'fixed-now' | 'fixed-hold' | 'leaving'
export type RowBusy = 'seen'

export const prefersReducedMotion = () => {
  try {
    return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

/** Tab counts from `counts`, or from the older `queue_totals` / `totals` while a deploy is mixed. */
export function countsFrom(response: ProblemsResponse): ProblemCounts {
  if (response.counts) return response.counts
  if (response.queue_totals) {
    return { open: response.queue_totals.needs_action, seen: response.queue_totals.acknowledged, fixed: response.queue_totals.resolved, legacy_seen: 0 }
  }
  const totals = response.totals
  return { open: totals?.open ?? 0, seen: totals?.acknowledged ?? 0, fixed: totals?.resolved ?? 0, legacy_seen: 0 }
}

/** Merges a fresh first page into the rows on screen: patch by id, add new ones, keep older pages. */
export function mergeRows(current: ProblemRow[], incoming: ProblemRow[], skip: ReadonlySet<number> = new Set()) {
  const currentIds = new Set(current.map((row) => row.id))
  // Rows this page just acted on are skipped (a refresh that started earlier may still list them).
  const kept = incoming.filter((row) => !skip.has(row.id))
  const keptIds = new Set(kept.map((row) => row.id))
  const arrived = kept.filter((row) => !currentIds.has(row.id))
  const missing = current.filter((row) => !keptIds.has(row.id))
  return { rows: [...kept, ...missing], arrived, missing }
}

const isAbort = (error: unknown) => error instanceof DOMException && error.name === 'AbortError'

export interface UseProblemsOptions {
  tab: ProblemTab
  /** revisions.alert (includes the 30 s resync). */
  revision?: number
  /** revisions.device: heartbeats. */
  deviceRevision?: number
}

export function useProblems(params: ProblemApiParams, { tab, revision = 0, deviceRevision = 0 }: UseProblemsOptions) {
  const key = useMemo(() => JSON.stringify(params), [params])
  const paramsRef = useRef(params)
  paramsRef.current = params
  const tabRef = useRef(tab)
  tabRef.current = tab
  const keyRef = useRef(key)

  const [rows, setRows] = useState<ProblemRow[]>([])
  const [counts, setCounts] = useState<ProblemCounts | null>(null)
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<unknown>(null)
  const [refreshError, setRefreshError] = useState<unknown>(null)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [switching, setSwitching] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [phases, setPhases] = useState<Record<number, RowPhase>>({})
  const [busy, setBusy] = useState<Record<number, RowBusy>>({})
  const [rowErrors, setRowErrors] = useState<Record<number, string>>({})
  const [arrived, setArrived] = useState<ProblemRow[]>([])

  // Always the latest rows, also between a setState and its render (a refresh
  // that lands right after an action must merge into the patched rows).
  const rowsRef = useRef(rows)
  const updateRows = useCallback((next: ProblemRow[] | ((current: ProblemRow[]) => ProblemRow[])) => {
    const value = typeof next === 'function' ? next(rowsRef.current) : next
    rowsRef.current = value
    setRows(value)
  }, [])
  const pagesRef = useRef(1)
  const removedRef = useRef(new Set<number>())
  const phasesRef = useRef(phases)
  const mainRequest = useRef<AbortController | null>(null)
  const timers = useRef(new Set<number>())

  useEffect(() => () => {
    timers.current.forEach((timer) => window.clearTimeout(timer))
    timers.current.clear()
    mainRequest.current?.abort()
  }, [])

  const later = useCallback((ms: number, run: () => void) => {
    if (ms <= 0) {
      run()
      return
    }
    const timer = window.setTimeout(() => {
      timers.current.delete(timer)
      run()
    }, ms)
    timers.current.add(timer)
  }, [])

  const setPhase = useCallback((id: number, phase: RowPhase | null) => {
    const current = phasesRef.current
    if (phase === null ? !(id in current) : current[id] === phase) return
    const next = { ...current }
    if (phase === null) delete next[id]
    else next[id] = phase
    phasesRef.current = next
    setPhases(next)
  }, [])

  const removeRow = useCallback((id: number) => {
    updateRows((current) => current.filter((row) => row.id !== id))
    setPhase(id, null)
  }, [setPhase, updateRows])

  /** Collapse a row out (200 ms), after an optional hold. Instant under reduced motion. */
  const leave = useCallback((id: number, hold: RowPhase | null, holdMs: number) => {
    const reduced = prefersReducedMotion()
    if (reduced) {
      removeRow(id)
      return
    }
    if (hold) setPhase(id, hold)
    later(hold ? holdMs : 0, () => {
      setPhase(id, 'leaving')
      later(LEAVE_MS, () => removeRow(id))
    })
  }, [later, removeRow, setPhase])

  const fetchPage = useCallback((cursor: string | null, signal: AbortSignal) =>
    api<ProblemsResponse>(`/api/v1/alerts${queryString({ ...paramsRef.current, cursor: cursor || undefined })}`, { signal }), [])

  // A filter change: one fresh first page; the old list stays dimmed meanwhile.
  useEffect(() => {
    keyRef.current = key
    mainRequest.current?.abort()
    const controller = new AbortController()
    mainRequest.current = controller
    removedRef.current = new Set()
    if (rowsRef.current.length) setSwitching(true)
    else setStatus('loading')
    setError(null)
    void fetchPage(null, controller.signal).then((response) => {
      if (controller.signal.aborted || keyRef.current !== key) return
      updateRows(Array.isArray(response.rows) ? response.rows : [])
      setCounts(countsFrom(response))
      setNextCursor(response.next_cursor ?? null)
      phasesRef.current = {}
      setPhases({})
      setRowErrors({})
      setArrived([])
      pagesRef.current = 1
      setStatus('ready')
      setRefreshError(null)
      setUpdatedAt(new Date())
    }).catch((reason) => {
      if (isAbort(reason) || keyRef.current !== key) return
      setError(reason)
      setStatus((current) => (rowsRef.current.length && current === 'ready' ? 'ready' : 'error'))
      if (rowsRef.current.length) setRefreshError(reason)
    }).finally(() => {
      if (mainRequest.current === controller) setSwitching(false)
    })
    return () => controller.abort()
  }, [fetchPage, key, updateRows])

  // Re-ask the first page with the same filters and merge it in place.
  const refresh = useCallback(async () => {
    const startedFor = keyRef.current
    const controller = new AbortController()
    setRefreshing(true)
    try {
      const response = await fetchPage(null, controller.signal)
      if (keyRef.current !== startedFor) return
      const incoming = Array.isArray(response.rows) ? response.rows : []
      const watchingOpen = tabRef.current !== 'fixed'
      const merged = mergeRows(rowsRef.current, incoming, watchingOpen ? removedRef.current : new Set())
      updateRows(merged.rows)
      setCounts(countsFrom(response))
      if (pagesRef.current <= 1) setNextCursor(response.next_cursor ?? null)
      setStatus('ready')
      setError(null)
      setRefreshError(null)
      setUpdatedAt(new Date())
      setArrived(merged.arrived)
      for (const row of merged.arrived) {
        setPhase(row.id, 'fresh')
        later(FRESH_MS, () => { if (phasesRef.current[row.id] === 'fresh') setPhase(row.id, null) })
      }
      // Open and Seen hold every open problem: one that left was fixed (or seen elsewhere).
      if (watchingOpen) {
        for (const row of merged.missing) {
          if (phasesRef.current[row.id] || removedRef.current.has(row.id)) continue
          leave(row.id, 'fixed-now', FIXED_NOW_MS)
        }
      }
    } catch (reason) {
      if (isAbort(reason) || keyRef.current !== startedFor) return
      // Background refreshes never pop a toast: a thin bar says what is shown.
      setRefreshError(reason)
    } finally {
      if (keyRef.current === startedFor) setRefreshing(false)
    }
  }, [fetchPage, later, leave, setPhase, updateRows])

  const runner = useThrottledRunner(refresh, LIVE_REFRESH_MS)
  const firstLive = useRef(true)
  useEffect(() => {
    if (firstLive.current) {
      firstLive.current = false
      return
    }
    runner.schedule()
  }, [revision, deviceRevision, runner])

  const loadMore = useCallback(async () => {
    if (!nextCursor) return
    const startedFor = keyRef.current
    const controller = new AbortController()
    setLoadingMore(true)
    try {
      const response = await fetchPage(nextCursor, controller.signal)
      if (keyRef.current !== startedFor) return
      const shown = new Set(rowsRef.current.map((row) => row.id))
      updateRows((current) => [...current, ...(response.rows || []).filter((row) => !shown.has(row.id))])
      setNextCursor(response.next_cursor ?? null)
      pagesRef.current += 1
    } catch (reason) {
      if (!isAbort(reason)) setRefreshError(reason)
    } finally {
      setLoadingMore(false)
    }
  }, [fetchPage, nextCursor, updateRows])

  const bumpCounts = useCallback((change: Partial<Record<'open' | 'seen' | 'fixed' | 'legacy_seen', number>>) => {
    setCounts((current) => {
      if (!current) return current
      const next = { ...current }
      for (const [name, delta] of Object.entries(change) as Array<[keyof typeof change, number]>) {
        next[name] = Math.max(0, (current[name] ?? 0) + delta)
      }
      return next
    })
  }, [])

  /** Mark as seen: that card only is busy; on New it collapses out; counts tick. */
  const markSeen = useCallback(async (row: ProblemRow, note?: string): Promise<'seen' | 'gone' | 'failed'> => {
    setBusy((current) => ({ ...current, [row.id]: 'seen' }))
    setRowErrors((current) => {
      if (!(row.id in current)) return current
      const next = { ...current }
      delete next[row.id]
      return next
    })
    try {
      const body = note && note.trim() ? { note: note.trim() } : {}
      const updated = await api<ProblemRow>(`/api/v1/alerts/${row.id}/acknowledge`, { method: 'POST', body: JSON.stringify(body) })
      updateRows((current) => current.map((item) => (item.id === row.id ? { ...item, ...updated, device: item.device, operator: item.operator } : item)))
      if (tabRef.current === 'new') {
        removedRef.current.add(row.id)
        leave(row.id, null, 0)
        bumpCounts({ open: -1, seen: 1 })
      }
      void runner.runNow()
      return 'seen'
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'ALERT_NOT_ACTIVE') {
        setRowErrors((current) => ({ ...current, [row.id]: 'gone' }))
        removedRef.current.add(row.id)
        leave(row.id, 'fixed-now', FIXED_NOW_MS)
        void runner.runNow()
        return 'gone'
      }
      setRowErrors((current) => ({ ...current, [row.id]: 'failed' }))
      return 'failed'
    } finally {
      setBusy((current) => {
        const next = { ...current }
        delete next[row.id]
        return next
      })
    }
  }, [bumpCounts, leave, runner, updateRows])

  /** After Mark as fixed: green tick for 1 s, then the card collapses; counts tick. */
  const fixedDone = useCallback((row: ProblemRow, response?: AlertResolveResponse | null) => {
    removedRef.current.add(row.id)
    if (response?.alert) {
      updateRows((current) => current.map((item) => (item.id === row.id ? { ...item, ...response.alert, device: item.device, operator: item.operator } : item)))
    }
    const seen = Boolean(row.acknowledged_at) || (row.state || '').toUpperCase() === 'ACKNOWLEDGED'
    bumpCounts((row.state || '').toUpperCase() === 'ACKNOWLEDGED'
      ? { legacy_seen: -1, fixed: 1 }
      : seen ? { seen: -1, fixed: 1 } : { open: -1, fixed: 1 })
    leave(row.id, 'fixed-hold', FIXED_HOLD_MS)
    void runner.runNow()
  }, [bumpCounts, leave, runner, updateRows])

  /** A refused Mark as fixed (409): the card's actions refresh from the next list load. */
  const refreshSoon = useCallback(() => { void runner.runNow() }, [runner])

  const retry = useCallback(() => runner.runNow(), [runner])

  return {
    rows, counts, nextCursor, status, error, refreshError, updatedAt, switching, refreshing, loadingMore,
    phases, busy, rowErrors, arrived,
    refresh: retry, loadMore, markSeen, fixedDone, refreshSoon,
  }
}

export type ProblemsData = ReturnType<typeof useProblems>
