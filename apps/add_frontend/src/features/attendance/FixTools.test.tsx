import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { queryClient } from '../../data'
import { expectPlainText } from '../../words/testing'
import { FixTools } from './FixTools'
import type { FixTool } from './useAttendanceFilters'

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const status = (overrides: Record<string, unknown> = {}) => ({
  allowed: true, enabled: false, active: false, changed_by: 'StateHealthAdmin', changed_at: '2026-11-05T12:00:00Z', reason: 'Maintenance', drain_active: false,
  backlog: { count: 1204, counted_at: '2026-11-05T13:59:20Z', remaining_estimate: 1204 },
  progress: { ready: 0, waiting_oracle: 0, in_flight: 0, confirmed_total: 10, confirmed_last_hour: 312, skipped_total: 0, skipped_window_hours: 24, skipped_by_reason: [], needs_review: 0 },
  rate_per_min: 40, eta_minutes: 30, batch_size: 50, min_interval_seconds: 5, in_flight_limit: 1500, backpressure: false,
  server_time: '2026-11-05T14:00:05Z', ...overrides,
})
const toast = { notice: vi.fn(), error: vi.fn() }
const allTime = { received: 616000, sent: 612340, waiting: 158, trying_again: 0, not_matched: 1204, set_aside: 2300, problem: 0, since: null }

function renderTools(tool: FixTool = '', extra: Partial<Parameters<typeof FixTools>[0]> = {}) {
  const props = {
    tool, onTool: vi.fn(), onShowMachineMatch: vi.fn(), onChooseByHand: vi.fn(), devices: [], toast, allTime,
    overviewLoading: false, deviceId: null, revision: 0, historyKind: 'all' as const, onHistoryKind: vi.fn(), releaseJobId: null, ...extra,
  }
  return { props, ...render(<FixTools {...props} />, { wrapper: BrowserRouter }) }
}

describe('Fix tools', () => {
  beforeEach(() => { queryClient.clear(); window.history.replaceState(null, '', '/attendance?view=fix-tools') })
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); queryClient.clear() })

  it('shows one card per tool with plain names and its own loading and error states', async () => {
    let fail = true
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/v2/attendance-force-releases') return response({ enabled: false, rows: [], next_cursor: null })
      if (path === '/api/v2/unknown-identity-auto-release') return fail ? response({ detail: 'down' }, 500) : response(status())
      return response({})
    }))
    const { container, props } = renderTools('', { allTime: null, overviewLoading: false })
    expect(screen.getByRole('article', { name: 'Send by hand' })).toBeTruthy()
    expect(screen.getByRole('article', { name: 'Send older not-matched check-ins (one area)' })).toBeTruthy()
    const recheck = screen.getByRole('article', { name: 'Re-check not-matched check-ins' })
    expect(await within(recheck).findByText("This tool isn't switched on yet.")).toBeTruthy()
    expect(within(recheck).queryByRole('button', { name: /Open/ })).toBeNull()
    expect(within(screen.getByRole('article', { name: 'All-time totals' })).getByText(/Couldn't load this/)).toBeTruthy()
    const auto = screen.getByRole('article', { name: 'Send not-matched check-ins automatically' })
    expect(await within(auto).findByText(/Couldn't load this\. Trying again every 10 seconds\./, undefined, { timeout: 5000 })).toBeTruthy()
    fail = false
    fireEvent.click(within(screen.getByRole('article', { name: 'Send by hand' })).getByRole('button', { name: 'Choose check-ins' }))
    expect(props.onChooseByHand).toHaveBeenCalled()
    fireEvent.click(within(screen.getByRole('article', { name: 'History' })).getByRole('button', { name: /Open/ }))
    expect(props.onTool).toHaveBeenCalledWith('history')
    expectPlainText(container)
  })

  it('shows the all-time totals from the cached overview', () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ enabled: true, rows: [] })))
    renderTools()
    expect(within(screen.getByRole('article', { name: 'All-time totals' })).getByText('612,340 sent · 158 waiting · 1,204 not matched · 2,300 set aside')).toBeTruthy()
  })

  it('starts Send all now in one click and asks for the password only to change the switch', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/v2/unknown-identity-auto-release/release-all' && init?.method === 'POST') return response({ ...status({ drain_active: true, active: true }), outcome: 'STARTED' }, 202)
      if (path === '/api/v2/unknown-identity-auto-release') return response(status())
      return response({ count: 0, devices: [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { props } = renderTools('auto-send')
    fireEvent.click(await screen.findByRole('button', { name: /Send all now/ }))
    await waitFor(() => expect(toast.notice).toHaveBeenCalledWith('Sending all now. Progress shows here.'))
    const post = fetchMock.mock.calls.find(([path, init]) => String(path).endsWith('/release-all') && init?.method === 'POST')
    expect(Object.keys(JSON.parse(String(post?.[1]?.body)))).toEqual(['idempotency_key'])
    expect(screen.queryByLabelText('Your password')).toBeNull()
    fireEvent.click(screen.getByRole('switch', { name: /Send automatically/ }))
    const dialog = screen.getByRole('dialog', { name: 'Turn on automatic sending' })
    expect(within(dialog).getByLabelText('Your password')).toBeTruthy()
    expect(within(dialog).getByLabelText('Why? (saved in the records)')).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: /Fix tools/ }))
    expect(props.onTool).toHaveBeenCalledWith('')
  })
})
