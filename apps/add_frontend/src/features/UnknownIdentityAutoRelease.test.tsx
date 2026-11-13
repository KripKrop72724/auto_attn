import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { queryClient } from '../data'
import { UnknownIdentityAutoRelease } from './UnknownIdentityAutoRelease'
import {
  autoReleaseHeadline,
  etaText,
  formatDuration,
  normalizeAutoReleaseStatus,
  type AutoReleaseStatus,
} from './unknownAutoRelease'

const PATH = '/api/v2/unknown-identity-auto-release'

const status = (overrides: Record<string, unknown> = {}) => ({
  allowed: true, enabled: true, active: true,
  changed_by: null, changed_at: null, reason: null,
  drain_active: false, drain_requested_at: null, drain_requested_by: null, drain_completed_at: null,
  backlog: { count: 108000, counted_at: '2026-11-05T13:59:20Z', remaining_estimate: 75000 },
  progress: {
    ready: 40, waiting_oracle: 1460, in_flight: 1500,
    queued_total: 33000, confirmed_total: 31000, confirmed_last_hour: 1800,
    skipped_total: 12000, skipped_window_hours: 24,
    skipped_by_reason: [
      { code: 'CNIC_CONFLICT', reason: 'Another CNIC or person is on record.', count: 9000 },
      { code: 'CLOCK_INVALID', reason: 'The terminal clock is invalid.', count: 3000 },
    ],
    needs_review: 12, checked_total: 45000, counted_at: '2026-11-05T13:59:30Z',
  },
  rate_per_min: 30, eta_minutes: 2500,
  last_batch_at: '2026-11-05T13:59:55Z', last_batch_size: 50, last_batch_job_id: 'job-1',
  batches_created: 900, punches_submitted: 45000,
  batch_size: 50, min_interval_seconds: 5, in_flight_limit: 1500, backpressure: false,
  last_error_code: null, last_error_at: null,
  server_time: '2026-11-05T14:00:05Z',
  ...overrides,
})

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

const toast = () => ({ notice: vi.fn(), error: vi.fn() })

describe('automatic release of unknown-identity punches', () => {
  beforeEach(() => queryClient.clear())
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    queryClient.clear()
  })

  it('shows ON by default with backlog age, Oracle queue, confirmations, skips and ETA', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(status())))
    render(<UnknownIdentityAutoRelease />)
    const control = await screen.findByRole('switch', { name: /Automatic release/i })
    expect(control.getAttribute('aria-checked')).toBe('true')
    expect(screen.getByText(/ON by default/)).toBeTruthy()
    expect(screen.getByText('ON · releasing automatically')).toBeTruthy()
    expect(screen.getByText('75,000')).toBeTruthy()
    expect(screen.getByText('counted 45 s ago')).toBeTruthy()
    expect(screen.queryByText(/in another release run or check/)).toBeNull()
    expect(screen.getByText('1,500')).toBeTruthy()
    expect(screen.getByText(/40 queued · 1,460 sent, awaiting confirmation/)).toBeTruthy()
    expect(screen.getByText('31,000')).toBeTruthy()
    expect(screen.getByText('1,800 in the last hour')).toBeTruthy()
    expect(screen.getByText('30/min')).toBeTruthy()
    expect(screen.getByText('about 41 h 40 min left')).toBeTruthy()
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('29')
    const skips = screen.getByText(/Why 12,000 punches were skipped/).closest('details') as HTMLElement
    expect(within(skips).getByText(/Another CNIC or person is on record/)).toBeTruthy()
    expect(within(skips).getByText('9,000')).toBeTruthy()
    expect(screen.getByText(/12 punches need a person to review/)).toBeTruthy()
    expect(screen.getByText(/Last batch 10 s ago: 50 punches/)).toBeTruthy()
    // ON: nothing to click, so no one-shot button.
    expect(screen.queryByRole('button', { name: /Release all now/i })).toBeNull()
  })

  it('says how many held punches another release run or check holds, not counted in Still to release', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(status({
      backlog: { count: 7482, counted_at: '2026-11-05T13:59:20Z', remaining_estimate: 20, held_by_other_approval: 7462 },
    }))))
    render(<UnknownIdentityAutoRelease />)
    const line = await screen.findByText('+ 7,462 in another release run or check (not counted above)')
    expect(line.closest('dd')?.querySelector('strong')?.textContent).toBe('20')
  })

  it('shows a counting state before the worker’s first count', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(status({
      backlog: { count: null, counted_at: null, remaining_estimate: null }, rate_per_min: null, eta_minutes: null,
    }))))
    render(<UnknownIdentityAutoRelease />)
    expect(await screen.findByText(/counting · the first count arrives within a minute/)).toBeTruthy()
    expect(screen.queryByRole('progressbar')).toBeNull()
    expect(screen.getByText(/speed is measured after the first count/)).toBeTruthy()
  })

  it('shows who turned it OFF and why, backpressure and errors, and starts Release all with one click', async () => {
    const off = status({
      enabled: false, active: false, changed_by: 'StateHealthAdmin', changed_at: '2026-11-05T12:00:00Z',
      reason: 'Oracle maintenance window', backpressure: true, last_error_code: 'ADD_BUSY', last_error_at: '2026-11-05T13:58:05Z',
    })
    let current: Record<string, unknown> = off
    const fetchMock = vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
      if (String(path) === `${PATH}/release-all` && init?.method === 'POST') {
        current = { ...off, active: true, drain_active: true, drain_requested_by: 'StateHealthAdmin', drain_requested_at: '2026-11-05T14:00:06Z' }
        return response({ ...current, outcome: 'STARTED' }, 202)
      }
      return response(current)
    })
    vi.stubGlobal('fetch', fetchMock)
    const notices = toast()
    const changed = vi.fn()
    render(<UnknownIdentityAutoRelease toast={notices} onChanged={changed} />)
    const control = await screen.findByRole('switch', { name: /Automatic release/i })
    expect(control.getAttribute('aria-checked')).toBe('false')
    expect(screen.getByText(/turned off by StateHealthAdmin/)).toBeTruthy()
    expect(screen.getByText('Oracle maintenance window')).toBeTruthy()
    expect(screen.getByText(/database was busy 2 min ago/)).toBeTruthy()
    // Backpressure only matters while something is releasing.
    expect(screen.queryByText(/Paused for Oracle/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Release all now/i }))
    await waitFor(() => expect(notices.notice).toHaveBeenCalledWith(expect.stringMatching(/Release all started/)))
    const post = fetchMock.mock.calls.find(([path, init]) => String(path) === `${PATH}/release-all` && init?.method === 'POST')
    const body = JSON.parse(String(post?.[1]?.body))
    expect(Object.keys(body)).toEqual(['idempotency_key'])
    expect(body.idempotency_key).toMatch(/^unknown-release-all:/)
    expect(changed).toHaveBeenCalled()
    expect(await screen.findByText('OFF · Release all running')).toBeTruthy()
    expect(screen.getByText(/Paused for Oracle: 1,500 punches are already waiting/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Release all now/i })).toBeNull()
  })

  it('lets an administrator stop a running Release all while the switch stays OFF', async () => {
    const draining = status({
      enabled: false, active: true, drain_active: true, changed_by: 'StateHealthAdmin', changed_at: '2026-11-05T12:00:00Z',
      drain_requested_by: 'Operator', drain_requested_at: '2026-11-05T13:00:00Z',
    })
    const fetchMock = vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
      if (String(path) === `${PATH}/control` && init?.method === 'POST') {
        return response(status({ enabled: false, active: false, drain_active: false, changed_by: 'StateHealthAdmin', changed_at: '2026-11-05T14:00:10Z' }))
      }
      return response(draining)
    })
    vi.stubGlobal('fetch', fetchMock)
    const notices = toast()
    render(<UnknownIdentityAutoRelease toast={notices} />)
    expect(await screen.findByText('OFF · Release all running')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Stop Release all/ }))
    const dialog = screen.getByRole('dialog', { name: 'Stop Release all now' })
    expect(dialog.textContent).toMatch(/stops the Release all now that is running/)
    expect(dialog.textContent).toMatch(/Automatic release stays OFF/)
    expect(dialog.textContent).toMatch(/cannot be unsent/)
    const submit = within(dialog).getByRole('button', { name: /Stop Release all/ })
    expect(submit.hasAttribute('disabled')).toBe(true)
    fireEvent.change(within(dialog).getByLabelText(/Audited reason/), { target: { value: 'Oracle is slow today' } })
    fireEvent.change(within(dialog).getByLabelText(/Administrator password/), { target: { value: 'right-password' } })
    fireEvent.click(submit)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(notices.notice).toHaveBeenCalledWith('Release all stopped; automatic release stays OFF. The change was audited.')
    const post = fetchMock.mock.calls.find(([path, init]) => String(path) === `${PATH}/control` && init?.method === 'POST')
    const body = JSON.parse(String(post?.[1]?.body))
    expect(body.enabled).toBe(false)
    expect(body.reason).toBe('Oracle is slow today')
    // The switch itself still offers only Turn ON.
    fireEvent.click(screen.getByRole('switch', { name: /Automatic release/i }))
    expect(screen.getByRole('dialog', { name: 'Turn automatic release ON' })).toBeTruthy()
  })

  it('says when every punch has been checked and the next full read is due', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(status({ resting_until: '2026-11-05T14:15:00Z' }))))
    render(<UnknownIdentityAutoRelease />)
    expect(await screen.findByText(/Every such punch has been checked/)).toBeTruthy()
    expect(screen.getByText(/new punches are still picked up/)).toBeTruthy()
  })

  it('says when the deployment blocks automatic release', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(status({ allowed: false, enabled: false, active: false }))))
    render(<UnknownIdentityAutoRelease />)
    expect(await screen.findByText(/blocked for this ADD deployment/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Release all now/i })).toBeNull()
  })

  it('requires a reason and the password to turn it OFF, explains queued punches still finish, and audits', async () => {
    const fetchMock = vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
      if (String(path) === `${PATH}/control` && init?.method === 'POST') {
        const sent = JSON.parse(String(init.body))
        if (sent.password !== 'right-password') return response({ detail: 'Step-up authentication failed' }, 403)
        return response(status({ enabled: false, active: false, changed_by: 'StateHealthAdmin', changed_at: '2026-11-05T14:00:10Z', reason: sent.reason }))
      }
      return response(status())
    })
    vi.stubGlobal('fetch', fetchMock)
    const notices = toast()
    render(<UnknownIdentityAutoRelease toast={notices} />)
    fireEvent.click(await screen.findByRole('switch', { name: /Automatic release/i }))
    const dialog = screen.getByRole('dialog', { name: 'Turn automatic release OFF' })
    expect(dialog.textContent).toMatch(/stops creating new release batches/)
    expect(dialog.textContent).toMatch(/Punches already queued \(1,500 punches now\) still go to Oracle/)
    expect(dialog.textContent).toMatch(/cannot be unsent/)
    const submit = within(dialog).getByRole('button', { name: /Turn OFF/ })
    expect(submit.hasAttribute('disabled')).toBe(true)

    fireEvent.change(within(dialog).getByLabelText(/Audited reason/), { target: { value: '  ok ' } })
    fireEvent.change(within(dialog).getByLabelText(/Administrator password/), { target: { value: 'wrong' } })
    expect(within(dialog).getByText(/2 \/ 500 characters · 1 more needed/)).toBeTruthy()
    expect(submit.hasAttribute('disabled')).toBe(true)

    fireEvent.change(within(dialog).getByLabelText(/Audited reason/), { target: { value: 'Oracle maintenance window' } })
    expect(submit.hasAttribute('disabled')).toBe(false)
    fireEvent.click(submit)
    expect(await within(dialog).findByRole('alert')).toBeTruthy()
    expect((within(dialog).getByLabelText(/Administrator password/) as HTMLInputElement).value).toBe('')

    fireEvent.change(within(dialog).getByLabelText(/Administrator password/), { target: { value: 'right-password' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /Turn OFF/ }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(notices.notice).toHaveBeenCalledWith('Automatic release is now OFF. The change was audited.')
    const posts = fetchMock.mock.calls.filter(([path, init]) => String(path) === `${PATH}/control` && init?.method === 'POST')
    expect(posts).toHaveLength(2)
    const bodies = posts.map(([, init]) => JSON.parse(String(init?.body)))
    expect(bodies[1]).toEqual({ enabled: false, reason: 'Oracle maintenance window', password: 'right-password', idempotency_key: bodies[0].idempotency_key })
    expect(bodies[1].idempotency_key).toMatch(/^unknown-auto-release-switch:/)
  })

  it('also asks for a reason and password to turn it back ON', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(status({ enabled: false, active: false, changed_by: 'StateHealthAdmin', changed_at: '2026-11-05T12:00:00Z' }))))
    render(<UnknownIdentityAutoRelease />)
    fireEvent.click(await screen.findByRole('switch', { name: /Automatic release/i }))
    const dialog = screen.getByRole('dialog', { name: 'Turn automatic release ON' })
    expect(dialog.textContent).toMatch(/Every punch still passes the Direct ORDS checks/)
    expect(within(dialog).getByLabelText(/Audited reason/)).toBeTruthy()
    expect(within(dialog).getByLabelText(/Administrator password/)).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('renders nothing for a non-administrator', async () => {
    const fetchMock = vi.fn(async () => response({ detail: 'Forbidden' }, 403))
    vi.stubGlobal('fetch', fetchMock)
    const view = render(<UnknownIdentityAutoRelease />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    await waitFor(() => expect(view.container.textContent).toBe(''))
  })
})

describe('automatic release helpers', () => {
  it('ignores answers that are not a status', () => {
    expect(normalizeAutoReleaseStatus({ rows: [] })).toBeNull()
    expect(normalizeAutoReleaseStatus(null)).toBeNull()
    // An older ADD without the held count reads as none held.
    expect(normalizeAutoReleaseStatus(status())?.backlog.held_by_other_approval).toBe(0)
  })

  it('formats durations and the Fleet headline', () => {
    expect(formatDuration(0.5)).toBe('under a minute')
    expect(formatDuration(45)).toBe('45 min')
    expect(formatDuration(120)).toBe('2 h')
    expect(formatDuration(60 * 24 * 3 + 60)).toBe('3 days 1 h')
    const on = normalizeAutoReleaseStatus(status()) as AutoReleaseStatus
    expect(autoReleaseHeadline(on)).toEqual({
      value: 'ON', tone: 'positive',
      detail: '75,000 left · 1,500 waiting for Oracle · 1,800 confirmed last hour · about 41 h 40 min left',
    })
    const off = normalizeAutoReleaseStatus(status({ enabled: false, changed_by: 'StateHealthAdmin', changed_at: '2026-11-05T13:00:05Z' })) as AutoReleaseStatus
    expect(autoReleaseHeadline(off)).toEqual({ value: 'OFF', tone: 'warning', detail: 'Turned off by StateHealthAdmin 1 h ago · 75,000 held' })
    const blocked = normalizeAutoReleaseStatus(status({ allowed: false })) as AutoReleaseStatus
    expect(autoReleaseHeadline(blocked).value).toBe('Blocked')
  })

  it('never says nothing is left while another release run or check holds punches', () => {
    const backlog = { count: 7462, counted_at: '2026-11-05T13:59:20Z', remaining_estimate: 0, held_by_other_approval: 7462 }
    const on = normalizeAutoReleaseStatus(status({ backlog, eta_minutes: 0 })) as AutoReleaseStatus
    expect(etaText(on)).toBe('nothing left here · 7,462 in another release run or check')
    expect(autoReleaseHeadline(on).detail).toBe(
      '0 left here · 7,462 in another release run or check · 1,500 waiting for Oracle · 1,800 confirmed last hour',
    )
    const off = normalizeAutoReleaseStatus(status({ backlog, eta_minutes: 0, enabled: false })) as AutoReleaseStatus
    expect(autoReleaseHeadline(off).detail).toBe('Turned off · 0 held · 7,462 in another release run or check')
    const none = normalizeAutoReleaseStatus(status({ backlog: { ...backlog, held_by_other_approval: 0 }, eta_minutes: 0 })) as AutoReleaseStatus
    expect(etaText(none)).toBe('nothing left to release')
  })
})
