import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AttendanceEvent, Device } from '../types'
import { AttendanceView } from './Attendance'

const device: Device = {
  connector_id: 'connector-one', hardware_id: 'hw-one', zone_id: 'ZONE-ONE', zone_name: 'Islamabad',
  device_id: '1', display_name: 'SLICTOWER · 3rd Floor', state: 'ONLINE', connected: true,
  firmware_version: '2.4.5', onboarding_generation: 2, last_onboarded_at: null,
  last_seen_at: '2026-08-12T10:00:00Z', current_activity: 'Live capture', last_error_code: null,
  zkt: {
    id: 1, serial: 'ADZV211860253', expected_serial: 'ADZV211860253', ip_address: '192.168.1.10',
    model: 'MB20', platform: 'ZLM60', online: true, connection_state: 'ONLINE', consecutive_failures: 0,
    consecutive_successes: 3, flap_count_15m: 0, last_transition_at: null, last_online_at: null,
    offline_since: null, stability_since: null, backoff_until: null, probe_latency_ms: 9,
    certification_state: 'CERTIFIED', certification_observations: 2, capabilities: {}, snapshot_complete: true,
    writes_disabled_reason: null, user_count: 2, attendance_count: 10, device_time: null,
    device_time_sampled_at: null, drift_seconds: 0, last_reconcile_at: null, next_restart_at: null,
  },
}

const event = (overrides: Partial<AttendanceEvent> = {}): AttendanceEvent => ({
  id: 1, event_uid: 'event-one', device_serial: 'ADZV211860253', uid: '7', user_id: '1007',
  display_name: 'Ayesha Khan', cnic_masked: '*****-****567-1', device_event_time: '2026-08-12T08:00:00Z',
  captured_at: '2026-08-12T08:00:01Z', received_at: '2026-08-12T08:00:02Z', source: 'LIVE',
  status: 'CAPTURED', punch: '0', clock_quality: 'OK', clock_drift_seconds: 2, ords_status: 'PENDING',
  oracle_confirmed_at: null, oracle_confirmation_path: null, identity_resolution_id: null,
  ...overrides,
})

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

class IntersectionObserverStub {
  static instance: IntersectionObserverStub | null = null
  readonly root = null
  readonly rootMargin = '0px'
  readonly thresholds = [0.7]
  constructor(private callback: IntersectionObserverCallback) { IntersectionObserverStub.instance = this }
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() { return [] }
  emit(isIntersecting: boolean) {
    this.callback([{ isIntersecting } as IntersectionObserverEntry], this as unknown as IntersectionObserver)
  }
}

const attendanceProps = {
  devices: [device], revision: 0, realtimeState: 'live' as const, realtimeLastSyncAt: null,
}

describe('Live attendance workspace', () => {
  beforeEach(() => {
    IntersectionObserverStub.instance = null
    vi.stubGlobal('IntersectionObserver', IntersectionObserverStub)
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('defaults to an all-source live chronology with loaded-result metrics and human labels', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => response({ rows: [event({
      ords_status: 'ACKNOWLEDGED', oracle_confirmed_at: '2026-08-12T08:00:05Z',
      oracle_confirmation_path: 'ADD_EVENT_UID',
    })], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)
    render(<AttendanceView {...attendanceProps} />)

    const row = await screen.findByRole('article', { name: /Ayesha Khan, Check in/i })
    expect(within(row).getByText('SLICTOWER · 3rd Floor')).toBeTruthy()
    expect(within(row).getByText('Live capture')).toBeTruthy()
    expect(within(row).getByText('Clock verified')).toBeTruthy()
    expect(within(row).getByText('ACKNOWLEDGED')).toBeTruthy()
    expect(screen.getByText('Events loaded').closest('article')?.textContent).toContain('1')
    expect(screen.getByText('Oracle confirmed').closest('article')?.textContent).toContain('1')
    expect(screen.getByText('Data-quality attention').closest('article')?.textContent).toContain('0')
    expect(String(fetchMock.mock.calls[0][0])).not.toContain('source=')

    fireEvent.click(within(row).getByLabelText(/view event details/i))
    expect(within(row).getByText('event-one')).toBeTruthy()
  })

  it('updates delivery state in place and queues unseen events when the reader is away from the top', async () => {
    const first = event()
    const second = event({ id: 2, event_uid: 'event-two', display_name: 'Bilal Ahmed', user_id: '1008', uid: '8', punch: '1' })
    let request = 0
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL) => response({
      rows: request++ === 0 ? [first] : [second, { ...first, ords_status: 'ACKNOWLEDGED', oracle_confirmed_at: '2026-08-12T08:01:00Z' }],
      next_cursor: null,
    })))
    const view = render(<AttendanceView {...attendanceProps} />)
    const ayesha = await screen.findByRole('article', { name: /Ayesha Khan, Check in/i })

    act(() => IntersectionObserverStub.instance?.emit(false))
    view.rerender(<AttendanceView {...attendanceProps} revision={1} />)

    expect(await screen.findByRole('button', { name: /1 new event · show newest/i })).toBeTruthy()
    expect(within(ayesha).getByText('ACKNOWLEDGED')).toBeTruthy()
    expect(screen.queryByRole('article', { name: /Bilal Ahmed/i })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /1 new event · show newest/i }))
    expect(await screen.findByRole('article', { name: /Bilal Ahmed, Check out/i })).toBeTruthy()
    expect(screen.getAllByRole('article', { name: /Ayesha Khan|Bilal Ahmed/i })).toHaveLength(2)
  })

  it('preserves loaded older rows and an exhausted pagination cursor during live reconciliation', async () => {
    const first = event()
    const older = event({ id: 3, event_uid: 'event-old', display_name: 'Older Event', user_id: '900', uid: '9' })
    let newestRequests = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.searchParams.get('cursor') === '10') return response({ rows: [older], next_cursor: null })
      newestRequests += 1
      return response({
        rows: newestRequests === 1 ? [first] : [{ ...first, ords_status: 'ACKNOWLEDGED', oracle_confirmed_at: '2026-08-12T08:01:00Z' }],
        next_cursor: 10,
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const view = render(<AttendanceView {...attendanceProps} />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })

    fireEvent.click(screen.getByRole('button', { name: /Load older events/i }))
    expect(await screen.findByRole('article', { name: /Older Event/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Load older events/i })).toBeNull()

    view.rerender(<AttendanceView {...attendanceProps} revision={1} />)
    await waitFor(() => expect(screen.getByText('ACKNOWLEDGED')).toBeTruthy())
    expect(screen.getByRole('article', { name: /Older Event/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Load older events/i })).toBeNull()
  })

  it('performs no revision fetch while paused and reconciles immediately on resume', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => response({ rows: [event()], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)
    const view = render(<AttendanceView {...attendanceProps} />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    expect(fetchMock).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    view.rerender(<AttendanceView {...attendanceProps} revision={1} />)
    await new Promise((resolve) => window.setTimeout(resolve, 350))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Updates paused')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(screen.getByText('Realtime connected')).toBeTruthy()
  })

  it('validates exact CNIC and PKT ranges before sending advanced filters', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => response({ rows: [event()], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)
    render(<AttendanceView {...attendanceProps} />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.click(screen.getByText(/^Filters$/i))

    fireEvent.change(screen.getByLabelText('Exact CNIC'), { target: { value: '35202' } })
    expect((await screen.findByRole('alert')).textContent).toMatch(/exact 13-digit cnic/i)
    expect(fetchMock).toHaveBeenCalledTimes(1)

    fireEvent.change(screen.getByLabelText('Exact CNIC'), { target: { value: '3520212345671' } })
    fireEvent.change(screen.getByLabelText('Capture source'), { target: { value: 'FULL_HISTORY' } })
    fireEvent.change(screen.getByLabelText('From (PKT)'), { target: { value: '2026-08-12T10:00' } })
    fireEvent.change(screen.getByLabelText('To (PKT)'), { target: { value: '2026-08-12T11:00' } })
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1))
    const sent = new URL(String(fetchMock.mock.calls.at(-1)?.[0]), 'https://add.test')
    expect(sent.searchParams.get('cnic')).toBe('3520212345671')
    expect(sent.searchParams.get('source')).toBe('FULL_HISTORY')
    expect(sent.searchParams.get('from_time')).toBe('2026-08-12T05:00:00.000Z')
    expect(sent.searchParams.get('to_time')).toBe('2026-08-12T06:00:00.000Z')

    fireEvent.change(screen.getByLabelText('From (PKT)'), { target: { value: '2026-08-12T12:00' } })
    expect((await screen.findByRole('alert')).textContent).toMatch(/from time must be earlier/i)
  })
})
