import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AttendanceRecoverySummary } from '../types'
import { AttendanceRecovery, notLinedUpTone } from './AttendanceRecovery'
import { notLinedUpText } from '../words/recover'
import { expectPlainText } from '../words/testing'

const delivery = (overrides: Partial<AttendanceRecoverySummary['delivery']>): AttendanceRecoverySummary['delivery'] => ({
  safe_retryable: 0,
  missing_outbox: 0,
  stale_in_flight: 0,
  identity_held: 0,
  permanent_review: 0,
  status_counts: {},
  ...overrides,
})

describe('not lined up for sending yet', () => {
  it('names held and set-aside rows apart and keeps both out of the number', () => {
    expect(notLinedUpText(delivery({ held_without_outbox: 74838, review_without_outbox: 307 })))
      .toBe(`If this stays above 0 for a day, tell IT. · 74,838 waiting to be matched to an employee · 307 set aside because something looks wrong`)
  })

  it('never calls set-aside rows unmatched', () => {
    expect(notLinedUpText(delivery({ held_without_outbox: 0, review_without_outbox: 3 })))
      .toBe('If this stays above 0 for a day, tell IT. · 3 set aside because something looks wrong')
    expect(notLinedUpText(delivery({ held_without_outbox: 5, review_without_outbox: 0 })))
      .toBe('If this stays above 0 for a day, tell IT. · 5 waiting to be matched to an employee')
    expect(notLinedUpText(delivery({ held_without_outbox: 0, review_without_outbox: 0 })))
      .toBe('If this stays above 0 for a day, tell IT.')
  })

  it('keeps the plain sentence when an older backend still counts held rows inside the number', () => {
    expect(notLinedUpText(delivery({ missing_outbox: 75145 }))).toBe('If this stays above 0 for a day, tell IT.')
    expect(notLinedUpText(undefined)).toBe('If this stays above 0 for a day, tell IT.')
  })

  it('stays calm when only held or set-aside rows have no sending slot', () => {
    expect(notLinedUpTone(delivery({ held_without_outbox: 74838, review_without_outbox: 0 }))).toBe('neutral')
    // ZKT invalid-time records never get a sending slot; the set-aside row
    // already flags them, so they must not keep this row amber for good.
    expect(notLinedUpTone(delivery({ held_without_outbox: 74838, review_without_outbox: 143 }))).toBe('neutral')
    expect(notLinedUpTone(undefined)).toBe('neutral')
  })

  it('turns amber for a real gap, whatever else has no sending slot', () => {
    expect(notLinedUpTone(delivery({ missing_outbox: 1 }))).toBe('warning')
    expect(notLinedUpTone(delivery({ missing_outbox: 1, held_without_outbox: 74838, review_without_outbox: 143 }))).toBe('warning')
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
    expect(screen.getByRole('list', { name: 'Where the check-ins are' })).toBeTruthy()
  })

  it('shows when the server computed the shared recovery figures', async () => {
    summary.metrics_computed_at = '2026-11-04T09:15:42Z'
    try {
      render(<AttendanceRecovery revision={0} toast={toast} />)
      await waitFor(() => expect(summaryCalls).toBe(1))
      expect(screen.queryByText(/Figures from/)).toBeNull()
      await releaseAll()
      const expected = '2:15 pm' // Pakistan time, no seconds
      await waitFor(() => expect(screen.getByText(`Figures from ${expected}`, { exact: false })).toBeTruthy())
    } finally {
      delete summary.metrics_computed_at
    }
  })

  it('shows plain rows, no retired resend form and no system words', async () => {
    const view = render(<AttendanceRecovery revision={0} toast={toast} />)
    await waitFor(() => expect(summaryCalls).toBe(1))
    await releaseAll()
    await screen.findByText('Waiting to be sent again')
    expect(screen.getByText('Not matched to an employee')).toBeTruthy()
    expect(screen.getByRole('link', { name: /Send not-matched check-ins automatically/ }).getAttribute('href')).toBe('/attendance?view=fix-tools&tool=auto-send')
    expect(screen.queryByText(/Preview filtered rows|Approve and queue batch|Audit reason/)).toBeNull()
    expect(screen.getByText('Fixing impossible times is switched off for now. Ask IT to switch it on.')).toBeTruthy()
    const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map(([input]) => String(input))
    expect(calls.some((url) => /attendance-recovery\/(preview|jobs)/.test(url))).toBe(false)
    expectPlainText(view.container)
  })
})

describe('Fix impossible times', () => {
  const toast = { toast: null, notice: vi.fn(), error: vi.fn(), dismiss: vi.fn() }
  const respond = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  it('sends corrected times typed in Pakistan time as UTC and never calls the retired endpoints', async () => {
    const posts: Array<{ path: string; body: Record<string, unknown> }> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (init?.method === 'POST') posts.push({ path, body: JSON.parse(String(init.body)) })
      if (path === '/api/v2/attendance-recovery/summary') return respond({ schema_version: '1', preview_enabled: true, execution_enabled: true, correction_enabled: true, delivery: delivery({}), source: { zkt_invalid_time: 1, zkt_malformed: 0, hikvision: {} } })
      if (path === '/api/v1/source-exceptions') return respond({ rows: [{ id: 7, display_name: 'ZONE-MULTAN-01', terminal_serial: 'RKQ1', ordinal: 11842, error_code: 'IMPLAUSIBLE_TERMINAL_TIME', observed_uid: '5', raw_record_digest: 'c'.repeat(64) }], totals: { open: 1 }, filtered_total: 1, next_cursor: null })
      if (path === '/api/v2/source-corrections/candidates') return respond({ rows: [] })
      if (path === '/api/v2/source-corrections/preview') return respond({ counts: { eligible: 1, rejected: 0 }, confirmation: 'CORRECT 1', candidate_digest: 'd', preview_expires_at: '2026-11-04T10:00:00Z', preview_signature: 's' })
      return respond({})
    }))
    render(<AttendanceRecovery revision={0} toast={toast} />)
    fireEvent.click(await screen.findByRole('checkbox'))
    fireEvent.change(screen.getByLabelText('Corrected time for record 11,842 (Pakistan time)'), { target: { value: '2026-11-04T09:00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Show the fixes' }))
    await waitFor(() => expect(posts.some((post) => post.path === '/api/v2/source-corrections/preview')).toBe(true))
    const preview = posts.find((post) => post.path === '/api/v2/source-corrections/preview')!
    expect(preview.body.corrections).toEqual([{ source_kind: 'ZKT_MANIFEST', source_ref: '7', corrected_device_time: '2026-11-04T04:00:00.000Z' }])
    // The safeguard stays: password and the typed phrase are still required to apply.
    const apply = await screen.findByRole('button', { name: 'Fix 1 time' }) as HTMLButtonElement
    expect(apply.disabled).toBe(true)
    expect(posts.some((post) => /attendance-recovery\/(preview|jobs)$/.test(post.path))).toBe(false)
  })
})
