import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { countsFrom, mergeRows, useProblems } from './useProblems'
import { dateWindow, toApiParams, PROBLEM_URL_SCHEMA } from './problemFilters'
import { readUrlValues } from '../../lib/urlState'
import type { ProblemRow, ProblemsResponse } from './types'

const row = (id: number, overrides: Partial<ProblemRow> = {}): ProblemRow => ({
  id,
  code: 'ZKT_CLOCK_DRIFT',
  severity: 'WARNING',
  state: 'OPEN',
  message: 'Clock drift.',
  details: {},
  first_seen_at: '2026-10-09T08:00:00Z',
  last_seen_at: '2026-10-09T09:00:00Z',
  acknowledged_at: null,
  resolved_at: null,
  device: { connector_id: `c-${id}`, display_name: `ZONE-X-${id}`, zone_id: `ZONE-X-${id}` },
  ...overrides,
})

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
const page = (rows: ProblemRow[], next: string | null = null): ProblemsResponse => ({ rows, next_cursor: next, counts: { open: rows.length, seen: 0, legacy_seen: 0, fixed: 0 } })

describe('mergeRows', () => {
  it('patches by id, adds new rows and keeps rows from later pages', () => {
    const current = [row(1), row(2), row(3)]
    const merged = mergeRows(current, [row(4), row(1, { severity: 'HIGH' }), row(2)])
    expect(merged.rows.map((item) => item.id)).toEqual([4, 1, 2, 3])
    expect(merged.rows[1].severity).toBe('HIGH')
    expect(merged.arrived.map((item) => item.id)).toEqual([4])
    expect(merged.missing.map((item) => item.id)).toEqual([3])
  })

  it('skips rows the page just acted on but keeps them on screen while they leave', () => {
    const merged = mergeRows([row(1), row(2)], [row(1), row(2)], new Set([1]))
    expect(merged.rows.map((item) => item.id)).toEqual([2, 1])
    expect(merged.arrived).toEqual([])
  })
})

describe('countsFrom', () => {
  it('reads counts, else the older queue totals, else the state totals', () => {
    expect(countsFrom({ rows: [], next_cursor: null, counts: { open: 1, seen: 2, legacy_seen: 3, fixed: 4 } }).legacy_seen).toBe(3)
    expect(countsFrom({ rows: [], next_cursor: null, queue_totals: { needs_action: 6, acknowledged: 736, resolved: 24939, all: 25681 } })).toEqual({ open: 6, seen: 736, fixed: 24939, legacy_seen: 0 })
    expect(countsFrom({ rows: [], next_cursor: null, totals: { all: 3, open: 1, acknowledged: 1, resolved: 1 } })).toEqual({ open: 1, seen: 1, fixed: 1, legacy_seen: 0 })
  })
})

describe('filters', () => {
  it('turns the URL into the API parameters', () => {
    const values = readUrlValues(PROBLEM_URL_SCHEMA, '?tab=seen&serious=low,urgent&type=machine,people&device=c-1&sort=oldest&att=1&old=1')
    expect(values.serious).toEqual(['urgent', 'low'])
    expect(toApiParams(values, 'health')).toEqual(expect.objectContaining({
      queue: 'LEGACY_SEEN', severity: 'CRITICAL,WARNING,MEDIUM,INFO', type: 'machine,people', connector_id: 'c-1',
      order: 'oldest', affects_attendance: 'true', division: 'health', limit: 500,
    }))
  })

  it('counts Pakistan days for the date windows', () => {
    const now = Date.parse('2026-11-06T20:30:00Z') // 1:30 am on Sat 7 Nov in Pakistan
    expect(dateWindow('today', '', '', now)).toEqual({ from_time: '2026-11-06T19:00:00.000Z' })
    expect(dateWindow('yesterday', '', '', now)).toEqual({ from_time: '2026-11-05T19:00:00.000Z', to_time: '2026-11-06T19:00:00.000Z' })
    expect(dateWindow('custom', '2026-11-01', '2026-11-06', now)).toEqual({ from_time: '2026-10-31T19:00:00.000Z', to_time: '2026-11-06T19:00:00.000Z' })
    expect(dateWindow('any', '', '', now)).toEqual({})
  })
})

describe('useProblems', () => {
  let responses: Array<(url: URL) => ProblemsResponse> = []
  beforeEach(() => {
    responses = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost')
      const next = responses.length > 1 ? responses.shift()! : responses[0]
      return json(next(url))
    }))
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('keeps "Show more" pages on a refresh and marks a row that left as fixed just now', async () => {
    responses = [() => page([row(1), row(2)], 'next'), () => page([row(3)])]
    const params = { queue: 'RESOLVED', limit: 2 }
    const { result } = renderHook(() => useProblems(params, { tab: 'fixed' }))
    await waitFor(() => expect(result.current.rows.map((item) => item.id)).toEqual([1, 2]))
    responses = [() => page([row(3)])]
    await act(async () => { await result.current.loadMore() })
    expect(result.current.rows.map((item) => item.id)).toEqual([1, 2, 3])
    responses = [() => page([row(9), row(1)], 'next')]
    await act(async () => { await result.current.refresh() })
    expect(result.current.rows.map((item) => item.id)).toEqual([9, 1, 2, 3])
  })

  it('gives a row that left New the "fixed just now" state', async () => {
    responses = [() => page([row(1), row(2)])]
    const params = { queue: 'NEEDS_ACTION', limit: 500 }
    const { result } = renderHook(() => useProblems(params, { tab: 'new' }))
    await waitFor(() => expect(result.current.rows).toHaveLength(2))
    responses = [() => page([row(1)])]
    await act(async () => { await result.current.refresh() })
    expect(result.current.phases[2]).toBe('fixed-now')
  })

  it('turns ten live changes in 2 s into one request now and one 10 s later', async () => {
    responses = [() => page([row(1)])]
    const params = { queue: 'NEEDS_ACTION', limit: 500 }
    let revision = 0
    const { result, rerender } = renderHook(() => useProblems(params, { tab: 'new', deviceRevision: revision }))
    await waitFor(() => expect(result.current.status).toBe('ready'))
    vi.useFakeTimers()
    const before = vi.mocked(fetch).mock.calls.length
    for (let index = 0; index < 10; index += 1) {
      revision += 1
      rerender()
      await act(async () => { vi.advanceTimersByTime(200) })
    }
    expect(vi.mocked(fetch).mock.calls.length - before).toBe(1)
    await act(async () => { vi.advanceTimersByTime(10_000) })
    expect(vi.mocked(fetch).mock.calls.length - before).toBe(2)
    vi.useRealTimers()
  })

  it('never lets an older answer overwrite newer filters', async () => {
    let releaseFirst: () => void = () => undefined
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost')
      if (url.searchParams.get('queue') === 'NEEDS_ACTION') {
        await new Promise<void>((resolve) => { releaseFirst = resolve })
        if (init?.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
        return json(page([row(1)]))
      }
      return json(page([row(5)]))
    })
    const first = { queue: 'NEEDS_ACTION', limit: 500 }
    const second = { queue: 'SEEN', limit: 500 }
    let params: Record<string, string | number> = first
    const { result, rerender } = renderHook(() => useProblems(params, { tab: 'new' }))
    params = second
    rerender()
    await waitFor(() => expect(result.current.rows.map((item) => item.id)).toEqual([5]))
    releaseFirst()
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(result.current.rows.map((item) => item.id)).toEqual([5])
  })
})
