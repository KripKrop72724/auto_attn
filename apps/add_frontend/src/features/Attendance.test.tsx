import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  AttendanceEvent,
  AttendanceReleaseCandidate,
  AttendanceReleaseCandidates,
  AttendanceReleaseQueueRow,
  AttendanceRepairJob,
  Device,
} from '../types'
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

const queueRow = (
  overrides: Partial<AttendanceReleaseQueueRow> = {},
): AttendanceReleaseQueueRow => ({
  connector_id: device.connector_id,
  device_id: device.device_id,
  device_name: device.display_name,
  user_key: '11111111-1111-4111-8111-111111111111',
  row_version: 3,
  display_name: 'Dr Farzana',
  user_id: '1007',
  uid: '7',
  cnic_masked: '*****-****567-1',
  eligible: true,
  lock_reason: null,
  lock_reasons: [],
  source_current: true,
  active_release_job_id: null,
  counts: {
    ordinary_blocked: 1,
    identity_reuse: 1,
    eligible: 2,
    locked: 0,
    in_progress: 0,
  },
  first_event_at: '2026-08-12T08:00:00Z',
  last_event_at: '2026-08-12T09:00:00Z',
  ...overrides,
})

const releaseCandidate = (
  overrides: Partial<AttendanceReleaseCandidate> = {},
): AttendanceReleaseCandidate => ({
  event_token: 'event-token-one',
  event_uid: 'release-event-one',
  device_event_time: '2026-08-12T08:00:00Z',
  punch: '0',
  status: '0',
  source: 'FULL_HISTORY',
  device_serial: 'ADZV211860253',
  uid: '**',
  user_id: '10**07',
  display_name: 'Dr Farzana',
  clock_quality: 'OK',
  source_ords_status: 'BLOCKED_IDENTITY',
  risk_class: 'ORDINARY_BLOCKED',
  evidence_classification: 'CURRENT_USER_LINEAGE',
  eligible: true,
  lock_reason: null,
  ...overrides,
})

const releaseCandidates = (
  rows: AttendanceReleaseCandidate[],
  overrides: Partial<AttendanceReleaseCandidates> = {},
): AttendanceReleaseCandidates => ({
  candidate_set_token: 'candidate-set-token',
  expires_at: '2026-08-12T10:15:00Z',
  source_current: true,
  source_certificate: { certificate_digest: 'c'.repeat(64) },
  target: {
    user_key: '11111111-1111-4111-8111-111111111111',
    row_version: 3,
    display_name: 'Dr Farzana',
    user_id: '1007',
    uid: '7',
    cnic_masked: '*****-****567-1',
    eligible: true,
    lock_reason: null,
  },
  filters: {
    date_from: null,
    date_to: null,
    hold_statuses: ['BLOCKED_IDENTITY', 'QUARANTINED_IDENTITY_REUSE'],
    punch: null,
    source: null,
  },
  totals: {
    all: rows.length,
    eligible: rows.filter((row) => row.eligible).length,
    locked: rows.filter((row) => !row.eligible).length,
    ordinary_blocked: rows.filter((row) => row.risk_class === 'ORDINARY_BLOCKED').length,
    identity_reuse: rows.filter((row) => row.risk_class === 'IDENTITY_REUSE').length,
  },
  rows,
  next_cursor: null,
  ...overrides,
})

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

describe('Live attendance workspace', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/attendance')
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

  it('combines Oracle status choices, missing CNIC, and a chosen page size', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => response({
      rows: [event()], next_cursor: null,
      status_options: ['BLOCKED_IDENTITY', 'FAILED_RETRYABLE', 'ACKED'],
    }))
    vi.stubGlobal('fetch', fetchMock)
    render(<AttendanceView {...attendanceProps} />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.click(screen.getByText(/^Filters$/i))
    fireEvent.click(screen.getByLabelText('BLOCKED IDENTITY'))
    fireEvent.click(screen.getByLabelText('FAILED RETRYABLE'))
    fireEvent.change(screen.getByLabelText('CNIC availability'), { target: { value: 'missing' } })
    fireEvent.change(screen.getByLabelText('Rows per load'), { target: { value: '250' } })
    await waitFor(() => {
      const sent = new URL(String(fetchMock.mock.calls.at(-1)?.[0]), 'https://add.test')
      expect(sent.searchParams.getAll('ords_status')).toEqual(['BLOCKED_IDENTITY', 'FAILED_RETRYABLE'])
      expect(sent.searchParams.get('cnic_present')).toBe('false')
      expect(sent.searchParams.get('limit')).toBe('250')
    })
    fireEvent.click(screen.getByRole('button', { name: /Oracle: BLOCKED IDENTITY/i }))
    await waitFor(() => {
      const sent = new URL(String(fetchMock.mock.calls.at(-1)?.[0]), 'https://add.test')
      expect(sent.searchParams.getAll('ords_status')).toEqual(['FAILED_RETRYABLE'])
    })
  })

  it('lets an administrator select a held punch when the synced user has a CNIC', async () => {
    const held = event({
      cnic_masked: null, ords_status: 'BLOCKED_IDENTITY',
      direct_ords_identity: { eligible: true, cnic_source: 'SYNCED_USER', exclusion: null },
    })
    vi.stubGlobal('fetch', vi.fn(async () => response({ rows: [held], next_cursor: null })))
    render(<AttendanceView {...attendanceProps} />)
    const row = await screen.findByRole('article', { name: /Ayesha Khan/i })
    expect(within(row).getByText(/CNIC on synced user/i)).toBeTruthy()
    fireEvent.click(within(row).getByRole('button', { name: 'Send to ORDS' }))
    const dialog = screen.getByRole('dialog')
    expect(dialog.textContent).toMatch(/current synced user CNIC because the saved punch has none/i)
    expect(dialog.textContent).toMatch(/does not prove who owned the terminal user ID/i)
  })

  it('keeps a punch without a usable current user CNIC out of manual selection', async () => {
    const held = event({
      cnic_masked: null, ords_status: 'BLOCKED_IDENTITY',
      direct_ords_identity: { eligible: false, cnic_source: null, exclusion: 'CNIC_MISSING' },
    })
    vi.stubGlobal('fetch', vi.fn(async () => response({ rows: [held], next_cursor: null })))
    render(<AttendanceView {...attendanceProps} />)
    const row = await screen.findByRole('article', { name: /Ayesha Khan/i })
    expect(within(row).queryByRole('button', { name: 'Send to ORDS' })).toBeNull()
    expect(within(row).getByRole('checkbox').hasAttribute('disabled')).toBe(true)
  })

  it('saves one administrator approval for a multi-punch Oracle send and shows durable progress', async () => {
    const second = event({ id: 2, event_uid: 'event-two', user_id: '1008', display_name: 'Bilal Ahmed' })
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
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
    render(<AttendanceView {...attendanceProps} />)
    await screen.findByRole('article', { name: /Bilal Ahmed/i })
    fireEvent.click(screen.getByLabelText('Select loaded punches'))
    fireEvent.click(screen.getByRole('button', { name: 'Send 2 punches' }))
    expect(screen.getByRole('dialog').textContent).toMatch(/unknown current users and users without a usable CNIC are skipped/i)
    fireEvent.change(screen.getByLabelText('Reason for sending'), { target: { value: 'Verified by administrator' } })
    fireEvent.change(screen.getByLabelText('Administrator password'), { target: { value: 'test-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Approve and send 2 punches' }))
    await screen.findByText('Preparing delivery')
    const call = fetchMock.mock.calls.find(([path, init]) => String(path) === '/api/v2/attendance-direct-ords' && init?.method === 'POST')
    const body = JSON.parse(String(call?.[1]?.body))
    expect(body.event_ids).toEqual([1, 2])
    expect(body.password).toBe('test-password')
    expect(body.reason).toBe('Verified by administrator')
    expect(body.idempotency_key).toMatch(/^direct-ords:/)
    expect(window.location.search).toContain('direct_run=saved-direct-run')
  })

  it('keeps the original hold visible beside the downstream-verified release state', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({
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
    render(<AttendanceView {...attendanceProps} />)

    const row = await screen.findByRole('article', { name: /Ayesha Khan, Check in/i })
    expect(within(row).getByText('BLOCKED IDENTITY')).toBeTruthy()
    expect(within(row).getByText('Released · Oracle verified')).toBeTruthy()
    expect(within(row).getByText(/Oracle and downstream verified/i)).toBeTruthy()
    fireEvent.click(within(row).getByLabelText(/view event details/i))
    expect(within(row).getAllByText(/blocked identity/i).length).toBeGreaterThan(1)
    expect(within(row).getByText(/job 22222222/i)).toBeTruthy()
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

    render(<AttendanceView {...attendanceProps} />)

    expect(await screen.findByText('Oracle content and downstream attendance are verified.')).toBeTruthy()
    expect(screen.queryByText('This punch cannot be released safely.')).toBeNull()
  })

  it('supports keyboard navigation across all three Attendance views and URL restoration', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname === '/api/v1/attendance')
        return response({ rows: [], next_cursor: null })
      if (['/api/v2/attendance-force-releases', '/api/v2/attendance-recovery/checks'].includes(url.pathname)) return response({ enabled: true, rows: [], next_cursor: null })
      if (url.pathname === '/api/v2/attendance-release-queue')
        return response({
          preview_enabled: true,
          execution_enabled: true,
          totals: { employees: 0, events: 0, eligible: 0, locked: 0 },
          rows: [],
          next_cursor: null,
        })
      if (url.pathname === '/api/v2/attendance-releases')
        return response({
          preview_enabled: true,
          execution_enabled: true,
          rows: [],
          next_cursor: null,
          totals: { all: 0, active: 0, attention: 0 },
          worker: {},
        })
      throw new Error(`Unexpected request ${url.pathname}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<AttendanceView {...attendanceProps} />)

    const allTab = screen.getByRole('tab', { name: /All events/i })
    const reviewTab = screen.getByRole('tab', { name: /Needs review/i })
    const historyTab = screen.getByRole('tab', { name: /Release history/i })
    expect(allTab.getAttribute('aria-selected')).toBe('true')

    fireEvent.keyDown(allTab, { key: 'ArrowRight' })
    expect(reviewTab.getAttribute('aria-selected')).toBe('true')
    expect(document.activeElement).toBe(reviewTab)
    expect(window.location.search).toBe('?view=needs-review')
    expect(await screen.findByRole('heading', { name: 'Attendance · Needs review' })).toBeTruthy()

    fireEvent.keyDown(reviewTab, { key: 'End' })
    expect(historyTab.getAttribute('aria-selected')).toBe('true')
    expect(document.activeElement).toBe(historyTab)
    expect(window.location.search).toBe('?view=release-history')
    expect(await screen.findByRole('heading', { name: 'Attendance · Release history' })).toBeTruthy()

    window.history.replaceState(null, '', '/attendance?view=needs-review')
    window.dispatchEvent(new PopStateEvent('popstate'))
    await waitFor(() => expect(reviewTab.getAttribute('aria-selected')).toBe('true'))
  })

  it('opens the manual workflow instead of the retired release controls', async () => {
    window.history.replaceState(null, '', '/attendance?view=needs-review')
    vi.stubGlobal('fetch', vi.fn(async () => response({ enabled: true, rows: [], next_cursor: null })))
    render(<AttendanceView {...attendanceProps} />)
    await screen.findByRole('heading', { name: 'Force release attendance' })
    expect(screen.queryByRole('button', { name: /Prepare.*punches|Start repair/ })).toBeNull()
    expect((screen.getByRole('button', { name: 'Sync and check' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
