import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  AttendanceEvent,
  AttendanceRepairJob,
  Device,
} from '../types'
import { AttendanceView } from './Attendance'
import { queryClient } from '../data'
import { autoReleaseQueryKey } from './unknownAutoRelease'
import { directSendDecisions } from './DirectOrdsSend'
import type { DirectOrdsPreviewRow } from '../types'
import type { AttendanceRow } from './attendance/types'
import { expectPlainText } from '../words/testing'
import { resetPrefs } from '../lib/prefs'

const device: Device = {
  connector_id: 'connector-one', hardware_id: 'hw-one', zone_id: 'ZONE-ONE', zone_name: 'Islamabad',
  device_id: '1', display_name: 'SLICTOWER · 3rd Floor', friendly_name: 'SLIC Tower · 3rd Floor', division: 'health',
  state: 'ONLINE', connected: true,
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

const connector = {
  connector_id: 'connector-one', display_name: 'SLICTOWER-3FL', zone_id: 'ZONE-SLICTOWER-3FL', zone_name: 'SLICTOWER-3FL',
  friendly_name: 'SLIC Tower · 3rd Floor', division: 'health' as const,
}

const event = (overrides: Partial<AttendanceRow> = {}): AttendanceEvent => ({
  id: 1, event_uid: 'event-one', device_serial: 'ADZV211860253', uid: '7', user_id: '1007',
  display_name: 'Ayesha Khan', cnic_masked: '*****-****567-1', device_event_time: '2026-08-12T08:00:00Z',
  captured_at: '2026-08-12T08:00:01Z', received_at: '2026-08-12T08:00:02Z', source: 'LIVE',
  status: 'CAPTURED', punch: '0', clock_quality: 'OK', clock_drift_seconds: 2, ords_status: 'PENDING',
  oracle_confirmed_at: null, oracle_confirmation_path: null, identity_resolution_id: null,
  source_connector: connector,
  ...overrides,
} as unknown as AttendanceEvent)

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

const summary = {
  from_time: '2026-08-11T19:00:00Z', to_time: null, computed_at: new Date().toISOString(), max_age_seconds: 15,
  totals: { received: 3412, sent: 3300, waiting: 80, trying_again: 12, not_matched: 28, set_aside: 3, problem: 1, oldest_waiting_at: null },
  last_sent_at: null,
  by_division: {
    life: { received: 1234, sent: 1200, waiting: 20, trying_again: 0, not_matched: 10, set_aside: 4, problem: 0, oldest_waiting_at: null },
    health: { received: 2178, sent: 2100, waiting: 60, trying_again: 12, not_matched: 18, set_aside: 0, problem: 1, oldest_waiting_at: null },
  },
}

const journey = {
  id: 1, delivery_group: 'sent', ords_status: 'ACKED', source: 'LIVE', live: true,
  device_event_time: '2026-08-12T08:00:00Z', captured_at: '2026-08-12T08:00:01Z', received_at: '2026-08-12T08:00:02Z',
  oracle_confirmed_at: '2026-08-12T08:00:05Z',
  outbox: { status: 'ACKED', delivery_type: 'LIVE', created_at: '2026-08-12T08:00:03Z', last_attempt_at: '2026-08-12T08:00:04Z', attempt_count: 1, next_attempt_at: null, acknowledged_at: '2026-08-12T08:00:05Z' },
  steps: [],
}

/** Wraps a page fetch stub so the totals and journey calls answer with their own shapes. */
const stub = (handler: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>) => vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(String(input), 'https://add.test')
  if (url.pathname === '/api/v1/attendance/summary') return response(summary)
  if (/^\/api\/v1\/attendance\/\d+\/journey$/.test(url.pathname)) return response(journey)
  return handler(input, init)
})

const listCalls = (fetchMock: ReturnType<typeof vi.fn>) => fetchMock.mock.calls
  .map(([input]) => new URL(String(input), 'https://add.test'))
  .filter((url) => url.pathname === '/api/v1/attendance')

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

const renderView = (props: Partial<Parameters<typeof AttendanceView>[0]> = {}) =>
  render(<AttendanceView {...attendanceProps} {...props} />, { wrapper: BrowserRouter })

const openRow = async (name: RegExp) => {
  const row = await screen.findByRole('article', { name })
  fireEvent.click(within(row).getByRole('button', { name: /^Open check-in/ }))
  return screen.findByRole('dialog')
}

const selectAllShown = async () => {
  fireEvent.click(screen.getByRole('button', { name: 'Select' }))
  fireEvent.click(await screen.findByLabelText('Select all shown'))
}

const sendChosen = () => fireEvent.click(within(screen.getByRole('region', { name: 'Chosen check-ins' })).getByRole('button', { name: 'Send to HR system' }))

const releaseJob = (
  overrides: Partial<AttendanceRepairJob> = {},
): AttendanceRepairJob => ({
  job_id: '22222222-2222-4222-8222-222222222222',
  connector_id: device.connector_id,
  device_id: device.device_id,
  actor: 'StateHealthAdmin',
  status: 'PREPARING_SOURCE',
  release_state: 'Preparing',
  phase: 'ORACLE_CLASSIFICATION',
  workflow_version: 'EVENT_SELECTION_V2',
  selection_mode: 'EXPLICIT',
  selection_manifest_digest: 'd'.repeat(64),
  release_target_user_id: '1007',
  date_scope: { timezone: 'Asia/Karachi', start_utc: null, end_utc_exclusive: null },
  request_digest: 'a'.repeat(64),
  preview_digest: null,
  preview_expires_at: null,
  source_dependency_job_id: null,
  totals: {
    employees: 1,
    events: 1,
    selected: 1,
    safe: 1,
    ordinary: 1,
    reuse: 0,
    safe_reuse: 0,
    excluded: 0,
    completed_employees: 0,
    completed_events: 0,
    attention_events: 0,
  },
  wait_reason: null,
  error_code: null,
  error_message: null,
  cancellation_requested: false,
  preparation_attempt_count: 0,
  next_attempt_at: null,
  created_at: '2026-08-12T09:00:00Z',
  approved_at: null,
  started_at: null,
  completed_at: null,
  targets: [{
    user_key: '11111111-1111-4111-8111-111111111111',
    display_name: 'Dr Farzana',
    cnic_masked: '*****-****567-1',
    expected_row_version: 3,
    desired_identity_digest: 'e'.repeat(64),
    status: 'FROZEN',
    event_count: 1,
    completed_event_count: 0,
    attention_event_count: 0,
  }],
  items: [],
  ...overrides,
})

describe('Attendance check-ins', () => {
  beforeEach(() => {
    // The automatic sending switch has its own tests; a saved "no status" keeps these fetch counts about the list.
    queryClient.setQueryData(autoReleaseQueryKey, null)
    resetPrefs()
    window.history.replaceState(null, '', '/attendance')
    IntersectionObserverStub.instance = null
    vi.stubGlobal('IntersectionObserver', IntersectionObserverStub)
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    queryClient.clear()
  })

  it('opens on today with server totals, one status per check-in and the journey in the sheet', async () => {
    const fetchMock = stub(async () => response({ rows: [event({
      ords_status: 'ACKED', delivery_group: 'sent', oracle_confirmed_at: '2026-08-12T08:00:05Z',
      oracle_confirmation_path: 'ADD_EVENT_UID',
    })], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)
    const { container } = renderView()

    const row = await screen.findByRole('article', { name: /Ayesha Khan, Check-in, .*, Sent/i })
    expect(within(row).getByText('SLIC Tower · 3rd Floor')).toBeTruthy()
    expect(within(row).getByText('Health')).toBeTruthy()
    expect(within(row).getByLabelText('Status: Sent')).toBeTruthy()
    expect(within(row).getByText(/^Sent /)).toBeTruthy()
    // How it arrived and the clock are only in the sheet now.
    expect(within(row).queryByText('Live capture')).toBeNull()
    expect(within(row).queryByText('Clock verified')).toBeNull()
    expect(row.textContent).not.toMatch(/retained event includes a terminal serial/)
    // Real totals for the day, never the loaded rows.
    expect(await screen.findByText('3,412')).toBeTruthy()
    expect(screen.getByText('Life 1,234 · Health 2,178')).toBeTruthy()
    const list = listCalls(fetchMock)
    expect(list).toHaveLength(1)
    // Today sends only the start of the Pakistan day, so the live list keeps moving.
    expect(list[0].searchParams.get('from_time')).toMatch(/T19:00:00\.000Z$/)
    expect(list[0].searchParams.get('to_time')).toBeNull()
    expect(list[0].searchParams.get('notify_summary')).toBe('false')
    expect(list[0].searchParams.get('source')).toBeNull()
    expectPlainText(container)

    const sheet = await openRow(/Ayesha Khan/)
    expect(window.location.search).toContain('event=1')
    expect(await within(sheet).findByText('Machine time correct')).toBeTruthy()
    expect(within(sheet).getByText('Live')).toBeTruthy()
    expect(within(sheet).getByText('Confirmed by HR system')).toBeTruthy()
    expect(within(sheet).getByText('event-one')).toBeTruthy()
    expect(within(sheet).getByText('event-one').closest('[data-technical]')).toBeTruthy()
    expect((sheet.querySelector('details[data-technical]') as HTMLDetailsElement).open).toBe(false)
  })

  it('names the place by the link box that delivered it without inventing a machine serial', async () => {
    vi.stubGlobal('fetch', stub(async () => response({ rows: [event({
      device_serial: null,
      source_connector: {
        connector_id: 'karachi-connector', display_name: 'ZONE-KARACHI-01',
        zone_id: 'ZONE-KARACHI', zone_name: 'Karachi', friendly_name: 'Karachi · Device 1', division: 'health',
      },
      terminal_provenance: {
        state: 'MISSING_TERMINAL_PROVENANCE', serial: null, confidence: 'REVIEW_REQUIRED',
        explanation: 'The saved punch has no verified terminal serial.',
      },
      source: 'DUMP_RECONNECT', ords_status: 'BLOCKED_IDENTITY',
    })], next_cursor: null })))
    renderView()

    const row = await screen.findByRole('article', { name: /Ayesha Khan, Check-in/i })
    expect(within(row).getByText('Came through Karachi · Device 1')).toBeTruthy()
    expect(within(row).getByText('Machine not confirmed')).toBeTruthy()
    expect(within(row).getByLabelText('Status: Not matched')).toBeTruthy()
    expect(within(row).queryByText(device.display_name)).toBeNull()
    expect(within(row).queryByText(device.zkt!.serial!)).toBeNull()
    expect(within(row).queryByText('SLIC Tower · 3rd Floor')).toBeNull()
    const sheet = await openRow(/Ayesha Khan/)
    // The backend sentence is only under Technical details.
    expect(within(sheet).getByText(/The saved punch has no verified terminal serial/).closest('[data-technical]')).toBeTruthy()
    expect(await within(sheet).findByText('Collected later, after the machine came back online')).toBeTruthy()
  })

  it('says the place is not known when the link box is unavailable', async () => {
    vi.stubGlobal('fetch', stub(async () => response({
      rows: [event({ device_serial: null, source_connector: null })], next_cursor: null,
    })))
    renderView()
    const row = await screen.findByRole('article', { name: /Ayesha Khan, Check-in/i })
    expect(within(row).getByText('Place not known')).toBeTruthy()
    expect(within(row).queryByText(device.display_name)).toBeNull()
  })

  it('updates the status in place and queues new check-ins while the reader is scrolled down', async () => {
    const first = event()
    const second = event({ id: 2, event_uid: 'event-two', display_name: 'Bilal Ahmed', user_id: '1008', uid: '8', punch: '1' })
    let request = 0
    vi.stubGlobal('fetch', stub(async () => response({
      rows: request++ === 0 ? [first] : [second, { ...first, ords_status: 'ACKED', oracle_confirmed_at: '2026-08-12T08:01:00Z' }],
      next_cursor: null,
    })))
    const view = renderView()
    const ayesha = await screen.findByRole('article', { name: /Ayesha Khan, Check-in/i })
    expect(within(ayesha).getByLabelText('Status: Waiting')).toBeTruthy()

    act(() => IntersectionObserverStub.instance?.emit(false))
    view.rerender(<AttendanceView {...attendanceProps} revision={1} />)

    expect(await screen.findByRole('button', { name: /1 new check-in · Show/i })).toBeTruthy()
    expect(within(screen.getByRole('article', { name: /Ayesha Khan/i })).getByLabelText('Status: Sent')).toBeTruthy()
    expect(screen.queryByRole('article', { name: /Bilal Ahmed/i })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /1 new check-in · Show/i }))
    expect(await screen.findByRole('article', { name: /Bilal Ahmed, Check-out/i })).toBeTruthy()
    expect(screen.getAllByRole('article', { name: /Ayesha Khan|Bilal Ahmed/i })).toHaveLength(2)
  })

  it('reads the list at most once for a burst of live check-ins', async () => {
    const fetchMock = stub(async () => response({ rows: [event()], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)
    const view = renderView()
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    for (let revision = 1; revision <= 20; revision += 1) view.rerender(<AttendanceView {...attendanceProps} revision={revision} />)
    await new Promise((resolve) => window.setTimeout(resolve, 400))
    // The first live refresh starts at once; the rest of the burst waits for the 3 s throttle.
    expect(listCalls(fetchMock).length).toBeLessThanOrEqual(2)
  })

  it('keeps older pages and an exhausted cursor during a live refresh', async () => {
    const first = event()
    const older = event({ id: 3, event_uid: 'event-old', display_name: 'Older Event', user_id: '900', uid: '9' })
    let newestRequests = 0
    const fetchMock = stub(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.searchParams.get('cursor') === '10') return response({ rows: [older], next_cursor: null })
      newestRequests += 1
      return response({
        rows: newestRequests === 1 ? [first] : [{ ...first, ords_status: 'ACKED', oracle_confirmed_at: '2026-08-12T08:01:00Z' }],
        next_cursor: 10,
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const view = renderView()
    await screen.findByRole('article', { name: /Ayesha Khan/i })

    fireEvent.click(screen.getByRole('button', { name: /Show older check-ins/i }))
    expect(await screen.findByRole('article', { name: /Older Event/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Show older check-ins/i })).toBeNull()

    view.rerender(<AttendanceView {...attendanceProps} revision={1} />)
    await waitFor(() => expect(screen.getByLabelText('Status: Sent')).toBeTruthy())
    expect(screen.getByRole('article', { name: /Older Event/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Show older check-ins/i })).toBeNull()
  })

  it('never lets a live refresh break into a filter change', async () => {
    const waiting = event({ id: 4, event_uid: 'event-waiting', display_name: 'Waiting Person', user_id: '901', uid: '10' })
    let release: () => void = () => undefined
    const fetchMock = stub(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.searchParams.getAll('delivery_group').includes('waiting')) {
        await new Promise<void>((resolve) => { release = resolve })
        if (init?.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
        return response({ rows: [waiting], next_cursor: null })
      }
      return response({ rows: [event()], next_cursor: 10 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const view = renderView()
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    await screen.findByText('3,412')
    fireEvent.click(screen.getByRole('button', { name: /^Waiting\s*80/ }))
    await waitFor(() => expect(listCalls(fetchMock).some((url) => url.searchParams.getAll('delivery_group').includes('waiting'))).toBe(true))
    const before = listCalls(fetchMock).length
    view.rerender(<AttendanceView {...attendanceProps} revision={1} />)
    await new Promise((resolve) => window.setTimeout(resolve, 50))
    expect(listCalls(fetchMock).length).toBe(before)
    await act(async () => { release() })
    expect(await screen.findByRole('article', { name: /Waiting Person/i })).toBeTruthy()
    expect(screen.queryByRole('article', { name: /Ayesha Khan/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Show older check-ins/i })).toBeNull()
  })

  it('reads nothing while the list is frozen and catches up when unfrozen', async () => {
    const fetchMock = stub(async () => response({ rows: [event()], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)
    const view = renderView()
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    expect(listCalls(fetchMock)).toHaveLength(1)
    expect(screen.getByText(/Newest first · Live/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Freeze list' }))
    view.rerender(<AttendanceView {...attendanceProps} revision={1} />)
    await new Promise((resolve) => window.setTimeout(resolve, 350))
    expect(listCalls(fetchMock)).toHaveLength(1)
    expect(screen.getByText(/Sending to the HR system goes on as normal/)).toBeTruthy()

    fireEvent.click(screen.getAllByRole('button', { name: 'Unfreeze list' })[0])
    await waitFor(() => expect(listCalls(fetchMock)).toHaveLength(2))
  })

  it('searches a 13-digit CNIC on the server but never writes it into the page address', async () => {
    const fetchMock = stub(async () => response({ rows: [event()], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)
    renderView()
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.change(screen.getByLabelText('Search check-ins'), { target: { value: '35202-1234567-1' } })
    await waitFor(() => expect(listCalls(fetchMock).at(-1)?.searchParams.get('cnic')).toBe('3520212345671'))
    expect(listCalls(fetchMock).at(-1)?.searchParams.get('q')).toBeNull()
    expect(screen.getByText('Searching by CNIC')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Remove filter: CNIC ending …671' })).toBeTruthy()
    await new Promise((resolve) => window.setTimeout(resolve, 300))
    expect(window.location.search).not.toMatch(/3520|cnic/i)

    fireEvent.change(screen.getByLabelText('Search check-ins'), { target: { value: 'Ayesha' } })
    await waitFor(() => expect(listCalls(fetchMock).at(-1)?.searchParams.get('q')).toBe('Ayesha'))
    await waitFor(() => expect(window.location.search).toContain('q=Ayesha'))
  })

  it('keeps the status, place and More filters in the address and sends them as list filters', async () => {
    const fetchMock = stub(async () => response({ rows: [event()], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)
    renderView()
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.click(screen.getByRole('button', { name: /^Status: Status/ }))
    fireEvent.click(await screen.findByLabelText(/^Not matched/))
    await waitFor(() => expect(listCalls(fetchMock).at(-1)?.searchParams.getAll('delivery_group')).toEqual(['not_matched']))
    expect(window.location.search).toContain('status=not_matched')

    fireEvent.click(screen.getByRole('button', { name: /More filters/ }))
    fireEvent.click(await screen.findByRole('switch', { name: 'No CNIC' }))
    fireEvent.click(screen.getByRole('switch', { name: 'Machine time was wrong' }))
    await waitFor(() => {
      const sent = listCalls(fetchMock).at(-1)!
      expect(sent.searchParams.getAll('ords_status')).toEqual(['CNIC_NOT_LINKED'])
      expect(sent.searchParams.getAll('clock_quality')).toEqual(['DRIFTED', 'INVALID'])
    })
    expect(window.location.search).toMatch(/no_id=1/)
    expect(window.location.search).not.toMatch(/cnic|password|reason|confirmation/i)

    fireEvent.click(screen.getByRole('button', { name: 'Remove filter: Not matched' }))
    await waitFor(() => expect(listCalls(fetchMock).at(-1)?.searchParams.getAll('delivery_group')).toEqual([]))
  })

  it('sends a tile choice as the status filter', async () => {
    const fetchMock = stub(async () => response({ rows: [event()], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)
    renderView()
    await screen.findByText('3,412')
    fireEvent.click(screen.getByRole('button', { name: /^Waiting\s*80/ }))
    await waitFor(() => expect(listCalls(fetchMock).at(-1)?.searchParams.getAll('delivery_group')).toEqual(['waiting']))
    expect(window.location.search).toContain('status=waiting')
  })

  it('sends Yesterday with an exclusive end at Pakistan midnight', async () => {
    window.history.replaceState(null, '', '/attendance?when=yesterday')
    const fetchMock = stub(async () => response({ rows: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)
    renderView()
    await waitFor(() => expect(listCalls(fetchMock)).toHaveLength(1))
    const sent = listCalls(fetchMock)[0]
    expect(sent.searchParams.get('to_exclusive')).toBe('true')
    expect(Date.parse(sent.searchParams.get('to_time')!) - Date.parse(sent.searchParams.get('from_time')!)).toBe(86_400_000)
    expect(await screen.findByText('No check-ins on these dates')).toBeTruthy()
  })

  it('lets an admin send a not-matched check-in when the machine has the CNIC', async () => {
    const held = event({
      cnic_masked: null, ords_status: 'BLOCKED_IDENTITY',
      direct_ords_identity: { eligible: true, cnic_source: 'SYNCED_USER', exclusion: null },
    })
    vi.stubGlobal('fetch', stub(async () => response({ rows: [held], next_cursor: null })))
    renderView()
    const row = await screen.findByRole('article', { name: /Ayesha Khan/i })
    expect(within(row).queryByText('No CNIC for this person')).toBeNull()
    const sheet = await openRow(/Ayesha Khan/)
    fireEvent.click(within(sheet).getByRole('button', { name: 'Send to HR system' }))
    const dialog = await screen.findByRole('dialog', { name: /Send 1 check-in to the HR system \(Oracle\)/ })
    expect(dialog.textContent).toMatch(/compares the CNIC the machine had for this Employee ID at the check-in time/i)
    expect(dialog.textContent).toMatch(/does not prove who had the Employee ID/i)
  })

  it('shows not-matched check-ins with a CNIC on the machine and sends a selection in one click', async () => {
    queryClient.removeQueries({ queryKey: autoReleaseQueryKey })
    const unknown = (id: number) => event({
      id, event_uid: `unknown-${id}`, display_name: null, cnic_masked: null, ords_status: 'BLOCKED_IDENTITY',
      direct_ords_identity: { eligible: true, cnic_source: 'SYNCED_USER', exclusion: null, punch_time_cnic: 'NONE', cnic_choice_required: true },
    })
    const fetchMock = stub(async (path: RequestInfo | URL, init?: RequestInit) => {
      const url = String(path)
      if (url === '/api/v2/attendance-direct-ords/unknown-synced') {
        return response({ count: 3, devices: [{ connector_id: device.connector_id, display_name: device.display_name, zone_id: 'ZONE-ONE', count: 3 }], event_ids: [], truncated: false })
      }
      if (url === '/api/v2/unknown-identity-auto-release') {
        return response({
          allowed: true, enabled: true, active: true, changed_by: null, changed_at: null, reason: null, drain_active: false,
          backlog: { count: 3, counted_at: '2026-08-12T10:00:00Z', remaining_estimate: 3 },
          progress: { ready: 0, waiting_oracle: 0, in_flight: 0, confirmed_total: 0, confirmed_last_hour: 0, skipped_total: 0, skipped_window_hours: 24, skipped_by_reason: [], needs_review: 0 },
          rate_per_min: 0, eta_minutes: null, batch_size: 50, min_interval_seconds: 5, in_flight_limit: 1500, backpressure: false,
          server_time: '2026-08-12T10:00:30Z',
        })
      }
      if (url === '/api/v2/attendance-force-releases') return response({ enabled: true, rows: [], next_cursor: null })
      if (url === '/api/v2/attendance-direct-ords/quick-release' && init?.method === 'POST') {
        const ids = JSON.parse(String(init.body)).event_ids as number[]
        return response({ job_id: 'quick-one', selected: ids.length, ready: ids.length - 1, skipped: 1, not_matching: 0, skipped_by_reason: { CNIC_CONFLICT: 1 } }, 202)
      }
      return response({ rows: url.includes('unknown_synced_cnic=true') ? [unknown(12), unknown(11), unknown(10)] : [event()], next_cursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderView()
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.click(screen.getByRole('tab', { name: /Fix tools/ }))
    const card = await screen.findByRole('article', { name: 'Send not-matched check-ins automatically' })
    await waitFor(() => expect(card.textContent).toMatch(/On · 3 still to send/))
    expect(fetchMock.mock.calls.some(([path]) => String(path).includes('unknown-synced'))).toBe(false)
    fireEvent.click(within(card).getByRole('button', { name: /^Open/ }))
    const control = await screen.findByRole('switch', { name: /Send automatically/i })
    expect(control.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Show these check-ins' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([path]) => String(path).includes('unknown_synced_cnic=true'))).toBe(true))
    expect(window.location.search).toContain('machine_match=1')
    const rows = await screen.findAllByRole('article', { name: /Name not known/i })
    expect(rows).toHaveLength(3)
    expect(within(rows[0]).getByText('Not matched · CNIC found on the machine, sending by itself')).toBeTruthy()
    expect(await screen.findByText(/SLIC Tower · 3rd Floor \(3\)/)).toBeTruthy()
    expect(fetchMock.mock.calls.some(([path]) => String(path) === '/api/v2/attendance-direct-ords/quick-release')).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Select' }))
    fireEvent.click(within(rows[0]).getByRole('checkbox'))
    fireEvent.click(within(rows[1]).getByRole('checkbox'))
    fireEvent.click(await screen.findByRole('button', { name: 'Send 2 now' }))
    expect(await screen.findByText(/on the way to the HR system/i)).toBeTruthy()
    const post = fetchMock.mock.calls.find(([path, init]) => String(path) === '/api/v2/attendance-direct-ords/quick-release' && init?.method === 'POST')
    const body = JSON.parse(String(post?.[1]?.body))
    expect(body.event_ids).toEqual([12, 11])
    expect(body.idempotency_key).toMatch(/^quick-release/)
    expect(Object.keys(body).sort()).toEqual(['event_ids', 'idempotency_key'])
    expect(screen.getByText(/1 check-in someone else had this Employee ID/)).toBeTruthy()
    expect(screen.queryByLabelText(/password/i)).toBeNull()
  })

  it('retries a busy one-click send of a selection with the same key', async () => {
    window.history.replaceState(null, '', '/attendance?machine_match=1')
    const unknown = event({
      id: 21, event_uid: 'unknown-21', display_name: null, cnic_masked: null, ords_status: 'BLOCKED_IDENTITY',
      direct_ords_identity: { eligible: true, cnic_source: 'SYNCED_USER', exclusion: null, punch_time_cnic: 'NONE', cnic_choice_required: true },
    })
    let attempts = 0
    const fetchMock = stub(async (path: RequestInfo | URL, init?: RequestInit) => {
      const url = String(path)
      if (url === '/api/v2/attendance-direct-ords/unknown-synced') return response({ count: 1, devices: [], event_ids: [21], truncated: false })
      if (url === '/api/v2/attendance-direct-ords/quick-release' && init?.method === 'POST') {
        attempts += 1
        if (attempts === 1) return response({ detail: { code: 'ADD_BUSY', message: 'ADD is busy delivering earlier punches. Retrying shortly.' } }, 503)
        return response({ job_id: 'quick-busy', selected: 1, ready: 1, skipped: 0, not_matching: 0, skipped_by_reason: {} }, 202)
      }
      return response({ rows: [unknown], next_cursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderView()
    const [row] = await screen.findAllByRole('article', { name: /Name not known/i })
    fireEvent.click(screen.getByRole('button', { name: 'Select' }))
    fireEvent.click(within(row).getByRole('checkbox'))
    fireEvent.click(await screen.findByRole('button', { name: 'Send 1 now' }))
    expect(await screen.findByText(/on the way to the HR system/i, undefined, { timeout: 8000 })).toBeTruthy()
    const posts = fetchMock.mock.calls.filter(([path, init]) => String(path) === '/api/v2/attendance-direct-ords/quick-release' && init?.method === 'POST')
    expect(posts).toHaveLength(2)
    const keys = posts.map(([, init]) => JSON.parse(String(init?.body)).idempotency_key)
    expect(keys[0]).toBe(keys[1])
    expect(screen.queryByText(/Stopped:/)).toBeNull()
  }, 15000)

  it('marks wrong machine times and CNIC changes with one short note', async () => {
    const invalid = event({
      ords_status: 'BLOCKED_IDENTITY', clock_quality: 'INVALID',
      direct_ords_identity: { eligible: false, cnic_source: 'SAVED_PUNCH', exclusion: 'CLOCK_INVALID', punch_time_cnic: null, cnic_choice_required: false },
    })
    const changed = event({
      id: 2, event_uid: 'changed-cnic', display_name: 'Toheed', user_id: '3980', cnic_masked: null,
      ords_status: 'BLOCKED_IDENTITY',
      direct_ords_identity: { eligible: true, cnic_source: 'SYNCED_USER', exclusion: null, punch_time_cnic: 'DIFFERS', cnic_choice_required: true },
    })
    vi.stubGlobal('fetch', stub(async () => response({ rows: [invalid, changed], next_cursor: null })))
    renderView()
    const invalidRow = await screen.findByRole('article', { name: /Ayesha Khan/i })
    expect(within(invalidRow).getByText('Wrong time on the machine · not sent until the time is fixed')).toBeTruthy()
    const changedRow = screen.getByRole('article', { name: /Toheed/i })
    expect(within(changedRow).getByText('CNIC changed · you choose which one to send')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Select' }))
    expect(within(invalidRow).getByRole('checkbox').hasAttribute('disabled')).toBe(true)
    expect(within(changedRow).getByRole('checkbox').hasAttribute('disabled')).toBe(false)
    const sheet = await openRow(/Ayesha Khan/)
    expect(within(sheet).queryByRole('button', { name: 'Send to HR system' })).toBeNull()
    expect(within(sheet).getByRole('link', { name: 'Fix the time in Recover attendance' }).getAttribute('href')).toBe('/recover?tab=held')
  })

  it('asks which CNIC to send, defaults to the check-in time CNIC and confirms imminent automatic sends', async () => {
    const rows = [
      event({ id: 11, event_uid: 'refused', display_name: 'Dr Farzana', user_id: '48384' }),
      event({ id: 12, event_uid: 'toheed-9', display_name: 'Toheed', user_id: '3980', cnic_masked: null, ords_status: 'BLOCKED_IDENTITY', direct_ords_identity: { eligible: true, cnic_source: 'SYNCED_USER' as const, exclusion: null, punch_time_cnic: 'DIFFERS' as const, cnic_choice_required: true } }),
      event({ id: 13, event_uid: 'saleem', display_name: 'A.Saleem', user_id: '04048', cnic_masked: null, ords_status: 'BLOCKED_IDENTITY', direct_ords_identity: { eligible: true, cnic_source: 'SYNCED_USER', exclusion: null, punch_time_cnic: 'NONE', cnic_choice_required: true } }),
      event({ id: 14, event_uid: 'auto-soon', display_name: 'Bilal', user_id: '1008', ords_status: 'BLOCKED_IDENTITY' }),
    ]
    const check = (overrides: Record<string, unknown>) => ({
      event_id: 0, user_id: '1', display_name: 'Someone', device_event_time: '2026-10-09T04:00:00Z',
      exclusion: null, exclusion_reason: null, cnic_source: 'SAVED_PUNCH', punch_time_cnic: null,
      punch_time_cnic_masked: null, punch_time_display_name: null, current_cnic_masked: '*****-****609-2',
      uid_matches_current: true, cnic_choice_required: false, cnic_choices: [], default_cnic_choice: null,
      auto_release_at_next_snapshot: false, awaiting_next_snapshot: false, database_guard: 'NOT_CHECKED',
      ...overrides,
    })
    const preview = { rows: [
      check({ event_id: 11, user_id: '48384', display_name: 'Dr Farzana', exclusion: 'TIME_IMPOSSIBLE', exclusion_reason: 'The punch time is impossible. It is never sent to Oracle.' }),
      check({ event_id: 12, user_id: '3980', display_name: 'Toheed', cnic_source: null, punch_time_cnic: 'DIFFERS', punch_time_cnic_masked: '*****-****609-0', cnic_choice_required: true, cnic_choices: ['PUNCH_TIME', 'CURRENT'], default_cnic_choice: 'PUNCH_TIME', decision_kind: 'CHOOSE_CNIC', decision_group: 'choose|0f3a', evidence_digest: '1'.repeat(64) }),
      check({ event_id: 13, user_id: '04048', display_name: 'A.Saleem', cnic_source: null, punch_time_cnic: 'NONE', record_position: 'NO_RECORDS', cnic_choice_required: true, cnic_choices: ['CURRENT'], decision_kind: 'CONFIRM_TODAY', decision_group: 'today|5be1', evidence_digest: '2'.repeat(64) }),
      check({ event_id: 14, user_id: '1008', display_name: 'Bilal', auto_release_at_next_snapshot: true, database_guard: 'ACCEPTED' }),
    ], missing_event_ids: [], counts: { selected: 4, refused: 1, cnic_choice_required: 2, auto_release_soon: 1 } }
    const fetchMock = stub(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/v2/attendance-direct-ords/preview') return response(preview)
      if (path === '/api/v2/attendance-direct-ords' && init?.method === 'POST') return response({
        job_id: 'checked-run', status: 'RUNNING', created_at: '2026-10-21T09:00:00Z', actor: 'operator',
        selected: 4, ready: 2, waiting: 0, confirmed: 0, skipped: 2, attention: 0,
      }, 202)
      if (path.includes('/api/v2/attendance-direct-ords')) return response({ rows: [], next_cursor: null })
      return response({ rows, next_cursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderView()
    await screen.findByRole('article', { name: /Toheed/i })
    await selectAllShown()
    sendChosen()
    const dialog = screen.getByRole('dialog')
    expect((screen.getByRole('button', { name: 'Send 4 check-ins' }) as HTMLButtonElement).disabled).toBe(true)
    const refused = await within(dialog).findByRole('region', { name: "Check-ins that can't be sent" })
    expect(refused.textContent).toMatch(/Dr Farzana.*impossible/)
    const changed = within(dialog).getByRole('region', { name: 'CNIC changed since the check-in' })
    const punchTime = within(changed).getByLabelText(/CNIC at check-in time \*{5}-\*{4}609-0/) as HTMLInputElement
    const today = within(changed).getByLabelText(/Today's CNIC \*{5}-\*{4}609-2/) as HTMLInputElement
    expect(punchTime.checked).toBe(true)
    expect(today.checked).toBe(false)
    expect(dialog.textContent).toMatch(/3 check-ins will not be sent; 1 check-in will be sent/)
    fireEvent.change(screen.getByLabelText('Why are you sending these? (saved in the records)'), { target: { value: 'HR confirmed identities' } })
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'test-password' } })

    fireEvent.click(within(dialog).getByLabelText(/Use today's CNIC \*{5}-\*{4}609-2 for A\.Saleem · Employee ID 04048 · 1 check-in/))
    fireEvent.click(within(dialog).getByLabelText(/Send 1 check-in now anyway/))
    fireEvent.click(today)
    fireEvent.click(screen.getByRole('button', { name: 'Send 4 check-ins' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => String(path) === '/api/v2/attendance-direct-ords' && init?.method === 'POST')).toBe(true))
    const call = fetchMock.mock.calls.find(([path, init]) => String(path) === '/api/v2/attendance-direct-ords' && init?.method === 'POST')
    const body = JSON.parse(String(call?.[1]?.body))
    expect(body.event_ids).toEqual([11, 12, 13, 14])
    // Each choice echoes the evidence the dialog showed.
    expect(body.cnic_choices).toEqual([
      { event_id: 12, choice: 'CURRENT', evidence_digest: '1'.repeat(64), held_by_other_confirmed: false },
      { event_id: 13, choice: 'CURRENT', evidence_digest: '2'.repeat(64), held_by_other_confirmed: false },
    ])
    expect(body.send_now_event_ids).toEqual([14])
  })

  it('sends the check-in time CNIC by default from the sheet and leaves unconfirmed check-ins unsent', async () => {
    const rows = [event({ id: 12, event_uid: 'toheed-9', display_name: 'Toheed', user_id: '3980', cnic_masked: null, ords_status: 'BLOCKED_IDENTITY', direct_ords_identity: { eligible: true, cnic_source: 'SYNCED_USER' as const, exclusion: null, punch_time_cnic: 'DIFFERS' as const, cnic_choice_required: true } })]
    const preview = { rows: [{
      event_id: 12, user_id: '3980', display_name: 'Toheed', device_event_time: '2026-10-09T04:00:00Z',
      exclusion: null, exclusion_reason: null, cnic_source: null, punch_time_cnic: 'DIFFERS',
      punch_time_cnic_masked: '*****-****609-0', punch_time_display_name: 'Toheed', current_cnic_masked: '*****-****609-2',
      uid_matches_current: true, cnic_choice_required: true, cnic_choices: ['PUNCH_TIME', 'CURRENT'], default_cnic_choice: 'PUNCH_TIME',
      auto_release_at_next_snapshot: false, awaiting_next_snapshot: false, database_guard: 'NOT_CHECKED',
      evidence_digest: '1'.repeat(64),
    }], missing_event_ids: [], counts: { selected: 1, refused: 0, cnic_choice_required: 1, auto_release_soon: 0 } }
    const fetchMock = stub(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/v2/attendance-direct-ords/preview') return response(preview)
      if (path === '/api/v2/attendance-direct-ords' && init?.method === 'POST') return response({
        job_id: 'punch-time-run', status: 'RUNNING', created_at: '2026-10-21T09:00:00Z', actor: 'operator',
        selected: 1, ready: 1, waiting: 0, confirmed: 0, skipped: 0, attention: 0,
      }, 202)
      if (path.includes('/api/v2/attendance-direct-ords')) return response({ rows: [], next_cursor: null })
      return response({ rows, next_cursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderView()
    const sheet = await openRow(/Toheed/)
    fireEvent.click(within(sheet).getByRole('button', { name: 'Send to HR system' }))
    const approve = await screen.findByRole('button', { name: 'Send 1 check-in' }) as HTMLButtonElement
    await screen.findByRole('region', { name: 'CNIC changed since the check-in' })
    fireEvent.change(screen.getByLabelText('Why are you sending these? (saved in the records)'), { target: { value: 'Punch-time identity' } })
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'test-password' } })
    expect(approve.disabled).toBe(false)
    fireEvent.click(approve)
    await screen.findByText('Getting ready')
    const call = fetchMock.mock.calls.find(([path, init]) => String(path) === '/api/v2/attendance-direct-ords' && init?.method === 'POST')
    const body = JSON.parse(String(call?.[1]?.body))
    expect(body.cnic_choices).toEqual([{ event_id: 12, choice: 'PUNCH_TIME', evidence_digest: '1'.repeat(64), held_by_other_confirmed: false }])
    expect(body.send_now_event_ids).toEqual([])
  })

  const previewRow = (overrides: Partial<DirectOrdsPreviewRow>): DirectOrdsPreviewRow => ({
    event_id: 0, zkt_device_id: 1, device_serial: 'KHI-3FL', user_id: '1', display_name: 'Someone',
    device_event_time: '2026-10-09T04:00:00Z', exclusion: null, exclusion_reason: null, cnic_source: null,
    punch_time_cnic: null, punch_time_cnic_masked: null, punch_time_display_name: null, other_cnics_masked: [],
    evidence_signals: [], current_cnic_masked: '*****-****609-2', uid_matches_current: true,
    identity_conflict_code: null, punch_time_cnic_held_by_other: false, cnic_choice_required: false,
    cnic_choices: [], default_cnic_choice: null, decision_kind: null, decision_group: null,
    auto_release_at_next_snapshot: false, awaiting_next_snapshot: false, database_guard: 'NOT_CHECKED',
    replaces_stopped_approval: false, evidence_digest: 'e'.repeat(64), ...overrides,
  })

  it('decides per group of identical evidence and never decides a punch unseen', () => {
    const toheed = Array.from({ length: 60 }, (_, index) => previewRow({
      event_id: 100 + index, user_id: '3980', display_name: 'Toheed', punch_time_cnic: 'DIFFERS',
      punch_time_cnic_masked: '*****-****609-0', cnic_choice_required: true, cnic_choices: ['PUNCH_TIME', 'CURRENT'],
      default_cnic_choice: 'PUNCH_TIME', decision_kind: 'CHOOSE_CNIC', decision_group: 'choose|1|3980|aaaa|0',
    }))
    const colleague = previewRow({
      event_id: 200, user_id: '4001', punch_time_cnic: 'DIFFERS', punch_time_cnic_masked: '*****-****111-1',
      punch_time_cnic_held_by_other: true, cnic_choice_required: true, cnic_choices: ['PUNCH_TIME', 'CURRENT'],
      default_cnic_choice: null, decision_kind: 'CHOOSE_CNIC', decision_group: 'choose|1|4001|bbbb|1',
    })
    const saleem = [301, 302].map((event_id) => previewRow({
      event_id, user_id: '04048', punch_time_cnic: 'NONE', cnic_choice_required: true, cnic_choices: ['CURRENT'],
      decision_kind: 'CONFIRM_TODAY', decision_group: 'today|1|04048',
    }))
    const gap = previewRow({
      event_id: 400, user_id: '3980', punch_time_cnic: 'CHANGED_NEAR_PUNCH', cnic_choice_required: true,
      cnic_choices: ['CURRENT'], other_cnics_masked: ['*****-****609-0'], evidence_signals: ['CNIC_CHANGED_NEAR_PUNCH'],
      decision_kind: 'CONFIRM_TODAY', decision_group: 'row|400',
    })
    const conflict = previewRow({ event_id: 500, punch_time_cnic: 'CONFLICT', exclusion: 'CNIC_CONFLICT', exclusion_reason: 'Another CNIC.' })
    const rows = [...toheed, colleague, ...saleem, gap, conflict]
    let result = directSendDecisions(rows, {}, {}, false)
    expect(result.choose.map((group) => group.rows.length)).toEqual([60, 1])
    expect(result.confirm.map((group) => group.key)).toEqual(['today|1|04048', 'row|400'])
    expect(result.cnicChoices).toHaveLength(60)
    expect(result.cnicChoices.every((item) => item.choice === 'PUNCH_TIME')).toBe(true)
    expect(result.skipped).toBe(5)  // the colleague's punch, A.Saleem's two, the gap and the conflict
    result = directSendDecisions(rows, { 'choose|1|4001|bbbb|1': 'CURRENT' }, { 'today|1|04048': true }, false)
    expect(result.cnicChoices.filter((item) => item.choice === 'CURRENT').map((item) => item.event_id)).toEqual([200, 301, 302])
    expect(result.skipped).toBe(2)
  })

  it('shows refused check-ins with the other CNIC and asks before using a CNIC someone else has', async () => {
    const hint = { eligible: true, cnic_source: 'SYNCED_USER' as const, exclusion: null, punch_time_cnic: 'NONE' as const, cnic_choice_required: true }
    const rows = [
      event({ id: 21, event_uid: 'conflict', display_name: 'Babar', user_id: '100', cnic_masked: null, ords_status: 'BLOCKED_IDENTITY', direct_ords_identity: hint }),
      event({ id: 22, event_uid: 'colleague', display_name: 'Toheed', user_id: '3980', cnic_masked: null, ords_status: 'BLOCKED_IDENTITY', direct_ords_identity: hint }),
      event({ id: 23, event_uid: 'gap', display_name: 'Toheed', user_id: '3980', cnic_masked: null, ords_status: 'BLOCKED_IDENTITY', direct_ords_identity: hint }),
    ]
    const preview = { rows: [
      previewRow({ event_id: 21, user_id: '100', display_name: 'Babar', punch_time_cnic: 'CONFLICT', exclusion: 'CNIC_CONFLICT', exclusion_reason: "ADD's records show another CNIC.", other_cnics_masked: ['*****-****777-1'], evidence_signals: ['OTHER_UID_RECORD'] }),
      previewRow({ event_id: 22, user_id: '3980', display_name: 'Toheed', punch_time_cnic: 'DIFFERS', punch_time_cnic_masked: '*****-****609-0', punch_time_cnic_held_by_other: true, evidence_signals: ['PUNCH_TIME_CNIC_HELD_BY_OTHER'], cnic_choice_required: true, cnic_choices: ['PUNCH_TIME', 'CURRENT'], default_cnic_choice: null, decision_kind: 'CHOOSE_CNIC', decision_group: 'choose|1|3980|aaaa|1' }),
      previewRow({ event_id: 23, user_id: '3980', display_name: 'Toheed', punch_time_cnic: 'CHANGED_NEAR_PUNCH', other_cnics_masked: ['*****-****609-0'], evidence_signals: ['CNIC_CHANGED_NEAR_PUNCH'], record_before_ended_at: '2026-09-25T09:00:00Z', record_after_started_at: '2026-10-19T08:01:00Z', cnic_choice_required: true, cnic_choices: ['CURRENT'], decision_kind: 'CONFIRM_TODAY', decision_group: 'row|23' }),
    ], missing_event_ids: [], counts: { selected: 3, refused: 1, cnic_choice_required: 2, auto_release_soon: 0 } }
    const fetchMock = stub(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/v2/attendance-direct-ords/preview') return response(preview)
      if (path === '/api/v2/attendance-direct-ords' && init?.method === 'POST') return response({
        job_id: 'conflict-run', status: 'RUNNING', created_at: '2026-10-21T09:00:00Z', actor: 'operator',
        selected: 3, ready: 1, waiting: 0, confirmed: 0, skipped: 2, attention: 0,
      }, 202)
      if (path.includes('/api/v2/attendance-direct-ords')) return response({ rows: [], next_cursor: null })
      return response({ rows, next_cursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderView()
    await screen.findByRole('article', { name: /Babar/i })
    await selectAllShown()
    sendChosen()
    const dialog = screen.getByRole('dialog')
    const refused = await within(dialog).findByRole('region', { name: "Check-ins that can't be sent" })
    expect(refused.textContent).toMatch(/Babar.*Another CNIC or person used this Employee ID.*\*{5}-\*{4}777-1/)
    const changed = within(dialog).getByRole('region', { name: 'CNIC changed since the check-in' })
    expect(changed.textContent).toMatch(/Another person on the machine has the old CNIC today/)
    expect((within(changed).getByLabelText(/CNIC at check-in time/) as HTMLInputElement).checked).toBe(false)
    expect((within(changed).getByLabelText(/Today's CNIC/) as HTMLInputElement).checked).toBe(false)
    const confirm = within(dialog).getByRole('region', { name: 'Use the current CNIC?' })
    // The old record ended weeks before the check-in: only today's CNIC is offered, with both edges shown.
    expect(confirm.textContent).toMatch(/CNIC changed around it \(the old CNIC's record ended .+; today's CNIC was first recorded .+\)\. Only a record within 30 minutes of the check-in is offered\..*other CNIC on record: \*{5}-\*{4}609-0/)
    expect(dialog.textContent).not.toMatch(/nearest/i)
    expect(dialog.textContent).toMatch(/3 check-ins will not be sent; 0 check-ins will be sent/)
    fireEvent.click(within(changed).getByLabelText(/CNIC at check-in time/))
    fireEvent.change(screen.getByLabelText('Why are you sending these? (saved in the records)'), { target: { value: 'HR confirmed Toheed punched' } })
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'test-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send 3 check-ins' }))
    await screen.findByText('Getting ready')
    const call = fetchMock.mock.calls.find(([path, init]) => String(path) === '/api/v2/attendance-direct-ords' && init?.method === 'POST')
    const body = JSON.parse(String(call?.[1]?.body))
    // Choosing the check-in time CNIC another person has is the explicit confirmation the server requires.
    expect(body.cnic_choices).toEqual([{ event_id: 22, choice: 'PUNCH_TIME', evidence_digest: 'e'.repeat(64), held_by_other_confirmed: true }])
  })

  it('asks per punch, with nothing preselected, when the records around a punch disagree', () => {
    const gap = previewRow({
      event_id: 600, user_id: '3980', display_name: 'Toheed', punch_time_cnic: 'CHANGED_NEAR_PUNCH',
      punch_time_cnic_masked: '*****-****609-0', punch_time_cnic_basis: 'RECORD_BEFORE_PUNCH', record_position: 'BETWEEN_RECORDS',
      cnic_choice_required: true, cnic_choices: ['PUNCH_TIME', 'CURRENT'], default_cnic_choice: null,
      decision_kind: 'CHOOSE_CNIC', decision_group: 'row|600', evidence_digest: '6'.repeat(64),
    })
    let result = directSendDecisions([gap], {}, {}, false)
    expect(result.choose).toHaveLength(1)
    expect(result.cnicChoices).toEqual([])
    expect(result.skipped).toBe(1)
    result = directSendDecisions([gap], { 'row|600': 'PUNCH_TIME' }, {}, false)
    expect(result.cnicChoices).toEqual([{ event_id: 600, choice: 'PUNCH_TIME', evidence_digest: '6'.repeat(64), held_by_other_confirmed: false }])
  })

  it('labels a CNIC change around a check-in by when each record ended and started, with nothing selected', async () => {
    const hint = { eligible: true, cnic_source: 'SYNCED_USER' as const, exclusion: null, punch_time_cnic: 'CHANGED_NEAR_PUNCH' as const, cnic_choice_required: true }
    const rows = [event({ id: 41, event_uid: 'edited', display_name: 'Toheed', user_id: '3980', cnic_masked: null, ords_status: 'BLOCKED_IDENTITY', direct_ords_identity: hint })]
    const preview = { rows: [previewRow({
      event_id: 41, user_id: '3980', display_name: 'Toheed', device_event_time: '2026-10-19T08:00:00Z',
      punch_time_cnic: 'CHANGED_NEAR_PUNCH', punch_time_cnic_masked: '*****-****609-0', punch_time_cnic_basis: 'RECORD_BEFORE_PUNCH',
      record_position: 'BETWEEN_RECORDS', record_before_ended_at: '2026-10-19T07:57:00Z', record_after_started_at: '2026-10-19T08:10:00Z',
      current_cnic_masked: '*****-****609-2', cnic_choice_required: true, cnic_choices: ['PUNCH_TIME', 'CURRENT'], default_cnic_choice: null,
      decision_kind: 'CHOOSE_CNIC', decision_group: 'row|41',
    })], missing_event_ids: [], counts: { selected: 1, refused: 0, cnic_choice_required: 1, auto_release_soon: 0 } }
    vi.stubGlobal('fetch', stub(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/v2/attendance-direct-ords/preview') return response(preview)
      if (path.includes('/api/v2/attendance-direct-ords')) return response({ rows: [], next_cursor: null })
      return response({ rows, next_cursor: null })
    }))
    renderView()
    await screen.findByRole('article', { name: /Toheed/i })
    await selectAllShown()
    sendChosen()
    const changed = await within(screen.getByRole('dialog')).findByRole('region', { name: 'CNIC changed since the check-in' })
    const old = within(changed).getByLabelText(/CNIC on today's person's record until/) as HTMLInputElement
    const today = within(changed).getByLabelText(/Today's CNIC \*{5}-\*{4}609-2 \(recorded from /) as HTMLInputElement
    expect(old.checked).toBe(false)
    expect(today.checked).toBe(false)
    expect(changed.textContent).toMatch(/the old CNIC's record ended .+; today's CNIC was first recorded .+, check-in /)
    expect(changed.textContent).not.toMatch(/nearest/i)
  })

  it('never sends a choice without the evidence it was made on', () => {
    const old = previewRow({
      event_id: 700, user_id: '04048', punch_time_cnic: 'NONE', cnic_choice_required: true, cnic_choices: ['CURRENT'],
      decision_kind: 'CONFIRM_TODAY', decision_group: 'today|aa', evidence_digest: null,
    })
    const result = directSendDecisions([old], {}, { 'today|aa': true }, false)
    expect(result.cnicChoices).toEqual([])
    expect(result.skipped).toBe(1)
  })

  it("explains check-ins older than the dashboard's first record and confirms them apart", async () => {
    const hint = { eligible: true, cnic_source: 'SYNCED_USER' as const, exclusion: null, punch_time_cnic: 'NONE' as const, cnic_choice_required: true }
    const rows = [
      event({ id: 31, event_uid: 'early', display_name: 'A.Saleem', user_id: '04048', cnic_masked: null, ords_status: 'BLOCKED_IDENTITY', direct_ords_identity: hint }),
      event({ id: 32, event_uid: 'named', display_name: 'A.Saleem', user_id: '04048', cnic_masked: null, ords_status: 'BLOCKED_IDENTITY', direct_ords_identity: hint }),
    ]
    const preview = { rows: [
      previewRow({ event_id: 31, user_id: '04048', display_name: 'A.Saleem', punch_time_cnic: 'NONE', record_position: 'BEFORE_FIRST_RECORD', first_observed_at: '2026-10-21T06:00:00Z', evidence_signals: ['PUNCH_BEFORE_FIRST_OBSERVATION'], cnic_choice_required: true, cnic_choices: ['CURRENT'], decision_kind: 'CONFIRM_TODAY', decision_group: 'today|early' }),
      previewRow({ event_id: 32, user_id: '04048', display_name: 'A.Saleem', punch_time_cnic: 'NONE', record_position: 'BETWEEN_RECORDS', evidence_signals: ['NAME_DIFFERS'], punch_display_name: 'Someone Else', cnic_choice_required: true, cnic_choices: ['CURRENT'], decision_kind: 'CONFIRM_TODAY', decision_group: 'row|32' }),
    ], missing_event_ids: [], counts: { selected: 2, refused: 0, cnic_choice_required: 2, auto_release_soon: 0 } }
    vi.stubGlobal('fetch', stub(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/v2/attendance-direct-ords/preview') return response(preview)
      if (path.includes('/api/v2/attendance-direct-ords')) return response({ rows: [], next_cursor: null })
      return response({ rows, next_cursor: null })
    }))
    renderView()
    await screen.findAllByRole('article', { name: /A\.Saleem/i })
    await selectAllShown()
    sendChosen()
    const confirm = await within(screen.getByRole('dialog')).findByRole('region', { name: 'Use the current CNIC?' })
    expect(within(confirm).getAllByRole('checkbox')).toHaveLength(2)
    expect(confirm.textContent).toMatch(/older than the dashboard's first record of this Employee ID/)
    expect(confirm.textContent).toMatch(/name saved with the check-in: Someone Else/)
  })

  it('keeps a check-in without a usable CNIC out of Send by hand', async () => {
    const held = event({
      cnic_masked: null, ords_status: 'BLOCKED_IDENTITY',
      direct_ords_identity: { eligible: false, cnic_source: null, exclusion: 'CNIC_MISSING' },
    })
    vi.stubGlobal('fetch', stub(async () => response({ rows: [held], next_cursor: null })))
    renderView()
    const row = await screen.findByRole('article', { name: /Ayesha Khan/i })
    expect(within(row).getByLabelText('Status: Not matched')).toBeTruthy()
    expect(within(row).getByText('No CNIC for this person')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Select' }))
    expect(within(row).getByRole('checkbox').hasAttribute('disabled')).toBe(true)
    const sheet = await openRow(/Ayesha Khan/)
    expect(within(sheet).queryByRole('button', { name: 'Send to HR system' })).toBeNull()
    const people = within(sheet).getByRole('link', { name: 'Open on People page' })
    // Only the Employee ID goes in the link, never a CNIC.
    expect(people.getAttribute('href')).toBe('/people/connector-one?q=1007')
  })

  it("uses the server's CNIC link for the note", async () => {
    const missing = event({
      display_name: 'Unlinked Employee', user_id: '14', cnic_masked: null,
      cnic_not_linked: true, ords_status: 'BLOCKED_IDENTITY',
      direct_ords_identity: { eligible: false, cnic_source: null, exclusion: 'CNIC_MISSING' },
    })
    const synced = event({
      id: 2, event_uid: 'synced-user', display_name: 'Synced Employee', user_id: '11',
      cnic_masked: null, cnic_not_linked: false, ords_status: 'BLOCKED_IDENTITY',
      direct_ords_identity: { eligible: true, cnic_source: 'SYNCED_USER', exclusion: null },
    })
    vi.stubGlobal('fetch', stub(async () => response({ rows: [missing, synced], next_cursor: null })))
    renderView()
    const missingRow = await screen.findByRole('article', { name: /Unlinked Employee/i })
    const syncedRow = screen.getByRole('article', { name: /Synced Employee/i })
    expect(within(missingRow).getByText('No CNIC for this person')).toBeTruthy()
    expect(within(syncedRow).queryByText('No CNIC for this person')).toBeNull()
  })

  it('shows the approved name for an unnamed check-in an admin sent, and nothing invented for others', async () => {
    const approvedIdentity = { display_name: 'Approved Employee', cnic_masked: '*****-****123-1' }
    const approval: NonNullable<AttendanceEvent['force_release']> = {
      run_id: 'approved-send', policy: 'manual-direct-ords-v1', administrator: 'StateHealthAdmin',
      approved_at: '2026-08-12T09:00:00Z', reason: 'Identified this punch', audit_id: 17,
      snapshot_id: null, sync_command_id: null, approved_identity: approvedIdentity,
    }
    const approved = event({
      display_name: null, cnic_masked: null, cnic_not_linked: false, force_release: approval,
      ords_status: 'ACKED', oracle_confirmed_at: '2026-08-12T09:01:00Z',
    })
    const unapproved = event({
      id: 2, event_uid: 'unapproved-punch', display_name: null, cnic_masked: null, ords_status: 'BLOCKED_IDENTITY',
      direct_ords_identity: { eligible: true, cnic_source: 'SYNCED_USER', exclusion: null },
    })
    vi.stubGlobal('fetch', stub(async () => response({ rows: [approved, unapproved], next_cursor: null })))
    renderView()
    const approvedRow = await screen.findByRole('article', { name: /Approved Employee, Check-in/i })
    expect(within(approvedRow).getByLabelText('Status: Sent')).toBeTruthy()
    expect(within(approvedRow).getByText(/^Sent by an admin/)).toBeTruthy()
    const unknownRow = screen.getByRole('article', { name: /Name not known, Check-in/i })
    expect(within(unknownRow).getByText('Employee ID 1007 · Check-in')).toBeTruthy()
    expect(within(unknownRow).queryByText('Approved Employee')).toBeNull()
    const sheet = await openRow(/Approved Employee/)
    const facts = within(sheet).getByRole('region', { name: 'Sent by an admin' })
    expect(facts.textContent).toMatch(/Sent by StateHealthAdmin on/)
    expect(facts.textContent).toMatch(/Reason: Identified this punch/)
    expect(within(facts).getByRole('link', { name: 'Open the sending task' }).getAttribute('href')).toBe('/attendance?direct_run=approved-send')
    expect(approved.display_name).toBeNull()
    expect(approved.cnic_masked).toBeNull()
  })

  it('saves one approval for a hand send of several check-ins and shows its progress', async () => {
    const second = event({ id: 2, event_uid: 'event-two', user_id: '1008', display_name: 'Bilal Ahmed' })
    const fetchMock = stub(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/v2/attendance-direct-ords' && init?.method === 'POST') return response({
        job_id: 'saved-direct-run', status: 'RUNNING', created_at: '2026-08-12T09:00:00Z', actor: 'operator',
        selected: 2, ready: 2, waiting: 0, confirmed: 0, skipped: 0, attention: 0,
      }, 202)
      if (path === '/api/v2/attendance-direct-ords') return response({ rows: [] })
      if (path.includes('/api/v2/attendance-direct-ords/')) return response({ rows: [], next_cursor: null })
      return response({ rows: [event(), second], next_cursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderView()
    await screen.findByRole('article', { name: /Bilal Ahmed/i })
    await selectAllShown()
    expect(screen.getByText('2 selected')).toBeTruthy()
    sendChosen()
    expect(screen.getByRole('dialog').textContent).toMatch(/Older damaged record numbers are checked against the HR system records/i)
    fireEvent.change(screen.getByLabelText('Why are you sending these? (saved in the records)'), { target: { value: 'Verified by administrator' } })
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'test-password' } })
    // Sending waits until the server has returned its read-only checks.
    const approve = screen.getByRole('button', { name: 'Send 2 check-ins' }) as HTMLButtonElement
    await waitFor(() => expect(approve.disabled).toBe(false))
    fireEvent.click(approve)
    await screen.findByText('Getting ready')
    const call = fetchMock.mock.calls.find(([path, init]) => String(path) === '/api/v2/attendance-direct-ords' && init?.method === 'POST')
    const body = JSON.parse(String(call?.[1]?.body))
    expect(body.event_ids).toEqual([1, 2])
    expect(body.password).toBe('test-password')
    expect(body.reason).toBe('Verified by administrator')
    expect(body.idempotency_key).toMatch(/^direct-ords:/)
    expect(window.location.search).toContain('direct_run=saved-direct-run')
  })

  it('rechecks a saved damaged-record task without sending anything new', async () => {
    window.history.replaceState(null, '', '/attendance?direct_run=saved-direct-run')
    const finished = {
      job_id: 'saved-direct-run', status: 'COMPLETED_WITH_REVIEW', created_at: '2026-08-12T09:00:00Z',
      actor: 'operator', selected: 5, ready: 0, waiting: 0, confirmed: 0, skipped: 0,
      attention: 5, legacy_recheckable: 5,
    }
    const fetchMock = stub(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.endsWith('/saved-direct-run/recheck') && init?.method === 'POST') {
        return response({ ...finished, status: 'WAITING_ORACLE', waiting: 5, attention: 0, legacy_recheckable: 0 }, 202)
      }
      if (path.endsWith('/saved-direct-run')) return response(finished)
      if (path.includes('/saved-direct-run/items')) return response({ rows: [], next_cursor: null })
      return response({ rows: [event()], next_cursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Check 5 check-ins in HR records' }))
    const dialog = screen.getByRole('dialog')
    expect(dialog.textContent).toMatch(/Nothing new is sent/i)
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'test-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check now' }))
    await screen.findByText('Waiting for the HR system')
    const call = fetchMock.mock.calls.find(([path, init]) => String(path).endsWith('/saved-direct-run/recheck') && init?.method === 'POST')
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ password: 'test-password' })
  })

  it('keeps the old release facts in Technical details only', async () => {
    vi.stubGlobal('fetch', stub(async () => response({
      rows: [event({
        ords_status: 'BLOCKED_IDENTITY',
        release_state: 'RELEASED',
        release_state_label: 'Released · Oracle verified',
        effective_identity_confirmed_at: '2026-08-12T08:05:00Z',
        effective_identity_downstream_confirmed_at: '2026-08-12T08:06:00Z',
        latest_release_job_id: '22222222-2222-4222-8222-222222222222',
      })],
      next_cursor: null,
    })))
    renderView()
    const row = await screen.findByRole('article', { name: /Ayesha Khan, Check-in/i })
    expect(within(row).getByLabelText('Status: Not matched')).toBeTruthy()
    expect(within(row).queryByText('Released · Oracle verified')).toBeNull()
    const sheet = await openRow(/Ayesha Khan/)
    const old = within(sheet).getByText(/Released · Oracle verified · job 22222222/)
    expect(old.closest('[data-technical]')).toBeTruthy()
  })

  it('describes completed release-history punches as verified rather than unsafe', async () => {
    const completed = releaseJob({
      status: 'COMPLETED',
      release_state: 'Released',
      phase: 'CERTIFIED',
      started_at: '2026-08-12T09:01:00Z',
      completed_at: '2026-08-12T09:02:00Z',
      totals: {
        employees: 1,
        events: 1,
        selected: 1,
        safe: 1,
        ordinary: 1,
        reuse: 0,
        safe_reuse: 0,
        excluded: 0,
        completed_employees: 1,
        completed_events: 1,
        attention_events: 0,
      },
      items: [{
        event_uid: 'release-event-one',
        user_key: '11111111-1111-4111-8111-111111111111',
        event_time: '2026-08-12T08:00:00Z',
        punch: '0',
        capture_source: 'FULL_HISTORY',
        source_ords_status: 'BLOCKED_IDENTITY',
        risk_class: 'ORDINARY_BLOCKED',
        selection_origin: 'EXPLICIT',
        state: 'COMPLETE',
        oracle_classification: 'MISSING',
        outcome: 'INSERTED_DOWNSTREAM_VERIFIED',
        attempt_count: 1,
        oracle_attempt_count: 1,
        downstream_attempt_count: 1,
        next_attempt_at: null,
        error_code: null,
        error_message: null,
        operation_id: '33333333-3333-4333-8333-333333333333',
        oracle_receipt_id: '33333333-3333-4333-8333-333333333333',
        downstream_status: 'VERIFIED',
        downstream_verified_at: '2026-08-12T09:02:00Z',
      }],
    })
    window.history.replaceState(
      null,
      '',
      `/attendance?view=release-history&release_job=${completed.job_id}`,
    )
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname === `/api/v2/attendance-releases/${completed.job_id}`)
        return response(completed)
      if (url.pathname === '/api/v2/attendance-releases')
        return response({
          preview_enabled: true,
          execution_enabled: true,
          rows: [completed],
          next_cursor: null,
          totals: { all: 1, active: 0, attention: 0 },
          worker: {},
        })
      throw new Error(`Unexpected request ${url.pathname}`)
    }))

    renderView()

    expect(await screen.findByText('Oracle content and downstream attendance are verified.')).toBeTruthy()
    expect(screen.queryByText('This punch cannot be released safely.')).toBeNull()
  })

  it('has two tabs, keeps them in the address and rewrites old links', async () => {
    const fetchMock = stub(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname === '/api/v1/attendance') return response({ rows: [], next_cursor: null })
      if (['/api/v2/attendance-force-releases', '/api/v2/attendance-recovery/checks', '/api/v2/attendance-direct-ords'].includes(url.pathname)) return response({ enabled: true, rows: [], next_cursor: null })
      if (url.pathname === '/api/v2/attendance-repairs') return response({ rows: [], next_cursor: null })
      return response({})
    })
    vi.stubGlobal('fetch', fetchMock)
    window.history.replaceState(null, '', '/attendance?view=needs-review&device_id=connector-one&user_key=abc')
    renderView()
    await waitFor(() => expect(Object.fromEntries(new URLSearchParams(window.location.search))).toEqual({ view: 'fix-tools', tool: 'held-staff', device_id: 'connector-one' }))
    expect(screen.getByRole('tab', { name: /Fix tools/ }).getAttribute('aria-selected')).toBe('true')
    expect(await screen.findByRole('heading', { name: 'Re-check not-matched check-ins' })).toBeTruthy()
    expect(screen.getByText('Needs your approval')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Prepare.*punches|Start repair/ })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Fix tools/, hidden: false }))
    expect(await screen.findByRole('article', { name: 'History' })).toBeTruthy()
    fireEvent.click(within(screen.getByRole('article', { name: 'History' })).getByRole('button', { name: /Open/ }))
    expect(window.location.search).toContain('tool=history')
    fireEvent.change(await screen.findByLabelText('Type'), { target: { value: 'old-repair' } })
    expect(await screen.findByText('Old repair tasks (no longer used)', { selector: 'option' })).toBeTruthy()
    expect(window.location.search).toContain('kind=old-repair')

    fireEvent.click(screen.getByRole('tab', { name: 'Check-ins' }))
    expect(window.location.search).not.toContain('view=')
    expect(await screen.findByText('No check-ins yet today')).toBeTruthy()
  })
})
