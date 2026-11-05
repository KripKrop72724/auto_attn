import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AttendanceRecoverySummary } from '../types'
import { AttendanceRecovery, withoutOutboxDetail, withoutOutboxTone } from './AttendanceRecovery'

const delivery = (overrides: Partial<AttendanceRecoverySummary['delivery']>): AttendanceRecoverySummary['delivery'] => ({
  safe_retryable: 0,
  missing_outbox: 0,
  stale_in_flight: 0,
  identity_held: 0,
  permanent_review: 0,
  status_counts: {},
  ...overrides,
})

describe('missing outbox detail', () => {
  it('names held and review rows apart and keeps both out of the missing count', () => {
    expect(withoutOutboxDetail(delivery({ held_without_outbox: 74838, review_without_outbox: 307 })))
      .toBe(`Deliverable rows to rebuild · ${(74838).toLocaleString()} held · ${(307).toLocaleString()} for review`)
  })

  it('never calls review rows held', () => {
    expect(withoutOutboxDetail(delivery({ held_without_outbox: 0, review_without_outbox: 3 })))
      .toBe('Deliverable rows to rebuild · 3 for review')
    expect(withoutOutboxDetail(delivery({ held_without_outbox: 5, review_without_outbox: 0 })))
      .toBe('Deliverable rows to rebuild · 5 held')
    expect(withoutOutboxDetail(delivery({ held_without_outbox: 0, review_without_outbox: 0 })))
      .toBe('Deliverable rows to rebuild')
  })

  it('keeps the original label when an older backend still counts held rows as missing', () => {
    expect(withoutOutboxDetail(delivery({ missing_outbox: 75145 }))).toBe('Durable rows to rebuild')
    expect(withoutOutboxDetail(undefined)).toBe('Durable rows to rebuild')
  })
})

describe('missing outbox tone', () => {
  it('stays positive when only held or review rows lack an outbox', () => {
    expect(withoutOutboxTone(delivery({ held_without_outbox: 74838, review_without_outbox: 0 }))).toBe('positive')
    // ZKT invalid-time quarantines never get an outbox; the Source review tile
    // already flags them, so they must not keep this tile amber for good.
    expect(withoutOutboxTone(delivery({ held_without_outbox: 74838, review_without_outbox: 143 }))).toBe('positive')
    expect(withoutOutboxTone(undefined)).toBe('positive')
  })

  it('turns amber for a real gap, whatever else lacks an outbox', () => {
    expect(withoutOutboxTone(delivery({ missing_outbox: 1 }))).toBe('warning')
    expect(withoutOutboxTone(delivery({ missing_outbox: 1, held_without_outbox: 74838, review_without_outbox: 143 }))).toBe('warning')
  })
})

describe('recovery summary refresh budget', () => {
  const toast = { toast: null, notice: vi.fn(), error: vi.fn(), dismiss: vi.fn() }
  const summary: AttendanceRecoverySummary = {
    schema_version: '1',
    preview_enabled: true,
    execution_enabled: false,
    delivery: delivery({ safe_retryable: 4, identity_held: 2, permanent_review: 1 }),
    source: { zkt_invalid_time: 0, zkt_malformed: 0, hikvision: {} },
  }
  let release: Array<() => void> = []
  let summaryCalls = 0
  const respond = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    release = []
    summaryCalls = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === '/api/v2/attendance-recovery/summary') {
        summaryCalls += 1
        // Hold every summary until the test releases it, like an overloaded server.
        await new Promise<void>((done) => release.push(done))
        return respond(summary)
      }
      if (path === '/api/v1/source-exceptions') return respond({ rows: [], totals: { open: 0 }, filtered_total: 0, next_cursor: null })
      if (path === '/api/v2/source-corrections/candidates') return respond({ rows: [] })
      return respond({})
    }))
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  const releaseAll = async () => {
    const pending = release
    release = []
    await act(async () => {
      pending.forEach((done) => done())
      await vi.advanceTimersByTimeAsync(0)
    })
  }

  it('never overlaps summary requests and refreshes for revisions at most every 15 s', async () => {
    const onSummary = vi.fn()
    const view = render(<AttendanceRecovery revision={0} toast={toast} onSummary={onSummary} />)
    await waitFor(() => expect(summaryCalls).toBe(1))

    // Revisions keep arriving while the first summary is still running.
    for (let revision = 1; revision <= 6; revision += 1) {
      view.rerender(<AttendanceRecovery revision={revision} toast={toast} onSummary={onSummary} />)
      await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    }
    expect(summaryCalls).toBe(1)

    await releaseAll()
    await waitFor(() => expect(onSummary).toHaveBeenCalledWith(summary))
    // Thirty seconds passed since the first start: one trailing refresh, not six.
    await waitFor(() => expect(summaryCalls).toBe(2))
    await releaseAll()

    view.rerender(<AttendanceRecovery revision={7} toast={toast} onSummary={onSummary} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(summaryCalls).toBe(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(6_000) })
    await waitFor(() => expect(summaryCalls).toBe(3))
    await releaseAll()
    expect(onSummary).toHaveBeenCalledTimes(3)
  })

  it('runs an explicit refresh at once without waiting for the throttle', async () => {
    const view = render(<AttendanceRecovery revision={0} refreshToken={0} toast={toast} />)
    await waitFor(() => expect(summaryCalls).toBe(1))
    await releaseAll()

    view.rerender(<AttendanceRecovery revision={0} refreshToken={1} toast={toast} />)
    await waitFor(() => expect(summaryCalls).toBe(2))
    // A second press while that request runs waits for it instead of stacking.
    view.rerender(<AttendanceRecovery revision={0} refreshToken={2} toast={toast} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(summaryCalls).toBe(2)
    await releaseAll()
    await waitFor(() => expect(summaryCalls).toBe(3))
    await releaseAll()
    expect(screen.getByRole('region', { name: 'Recovery health' })).toBeTruthy()
  })

  it('shows when the server computed the shared recovery figures', async () => {
    summary.metrics_computed_at = '2026-11-04T09:15:42Z'
    try {
      render(<AttendanceRecovery revision={0} toast={toast} />)
      await waitFor(() => expect(summaryCalls).toBe(1))
      expect(screen.queryByText(/Figures as of/)).toBeNull()
      await releaseAll()
      const expected = new Date('2026-11-04T09:15:42Z').toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
      await waitFor(() => expect(screen.getByText(`Figures as of ${expected}`, { exact: false })).toBeTruthy())
    } finally {
      delete summary.metrics_computed_at
    }
  })
})
