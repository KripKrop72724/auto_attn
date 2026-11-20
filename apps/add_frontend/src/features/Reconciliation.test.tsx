import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BrowserRouter } from 'react-router-dom'
import { useToast } from '../App'
import { friendlyNames } from '../lib/deviceNames'
import { expectPlainText } from '../words/testing'
import type {
  Device,
  ReconciliationJob,
  SourceException,
  SourceExceptionAssurance,
} from '../types'
import { ReconciliationView } from './Reconciliation'

const device: Device = {
  connector_id: 'multan-connector',
  hardware_id: 'multan-hardware',
  zone_id: 'ZONE-MULTAN-01',
  zone_name: 'Multan',
  device_id: '17',
  display_name: 'ZONE-MULTAN-01',
  state: 'ONLINE',
  connected: true,
  firmware_version: '2.4.5',
  onboarding_generation: 2,
  last_onboarded_at: null,
  last_seen_at: '2026-08-21T15:20:00Z',
  current_activity: 'Oracle assurance',
  last_error_code: null,
  zkt: {
    id: 17,
    serial: 'RKQ4245100152',
    expected_serial: 'RKQ4245100152',
    ip_address: '192.168.1.201',
    model: 'MB20',
    platform: 'ZLM60',
    online: true,
    connection_state: 'ONLINE',
    consecutive_failures: 0,
    consecutive_successes: 4,
    flap_count_15m: 0,
    last_transition_at: null,
    last_online_at: null,
    offline_since: null,
    stability_since: null,
    backoff_until: null,
    probe_latency_ms: 8,
    certification_state: 'CERTIFIED',
    certification_observations: 2,
    capabilities: {},
    snapshot_complete: true,
    writes_disabled_reason: null,
    user_count: 36,
    attendance_count: 11843,
    device_time: null,
    device_time_sampled_at: null,
    drift_seconds: 0,
    last_reconcile_at: null,
    next_restart_at: null,
  },
}

const assurance = (
  overrides: Partial<SourceExceptionAssurance> = {},
): SourceExceptionAssurance => ({
  total: 1,
  reviewed: 0,
  open: 1,
  invalid_time: 1,
  malformed: 0,
  state: 'REVIEW_REQUIRED',
  cohort_digest: 'a'.repeat(64),
  ...overrides,
})

const job = (
  sourceExceptionAssurance = assurance(),
): ReconciliationJob => ({
  job_id: 'f0387860-dd4f-4879-bcc1-30dc58c5788f',
  mode: 'FULL_HISTORY_BASELINE',
  status: sourceExceptionAssurance.open ? 'NEEDS_ATTENTION' : 'RUNNING',
  phase: sourceExceptionAssurance.open ? 'FINAL_ASSURANCE' : 'DRAINING_ORDS',
  wait_reason: sourceExceptionAssurance.open
    ? 'SOURCE_QUARANTINE_REQUIRES_REVIEW'
    : null,
  error_code: sourceExceptionAssurance.open
    ? 'SOURCE_QUARANTINE_REQUIRES_REVIEW'
    : null,
  error_message: sourceExceptionAssurance.open
    ? '1 of 1 preserved source record requires administrator review.'
    : null,
  operator_state: sourceExceptionAssurance.open
    ? 'REVIEW_REQUIRED'
    : 'VERIFYING_ORACLE',
  operator_message: sourceExceptionAssurance.open
    ? 'Review the certified source exception to continue automatically.'
    : 'Reviewed exclusions are preserved; Oracle assurance is continuing.',
  completion_outcome: null,
  review_required: sourceExceptionAssurance.reviewed > 0,
  source_exception_assurance: sourceExceptionAssurance,
  connector: {
    connector_id: device.connector_id,
    device_id: device.device_id,
    display_name: device.display_name,
    zone_id: device.zone_id,
    connected: true,
  },
  terminal: {
    serial: 'RKQ4245100152',
    generation: 1,
    cutoff_count: 11843,
    latest_count: 11843,
    record_size: 40,
    source_total_bytes: 473724,
  },
  progress: {
    scanned: 11843,
    remaining: 0,
    add_durable: 11843,
    already_present: 0,
    terminal_duplicates: 0,
    blocked_identity: 3081,
    quarantined: 1,
    oracle_target: 8760,
    oracle_confirmed: 2705,
    oracle_pending: 6055,
    oracle_review_required: 0,
    retry_count: 0,
    auto_retry_count: 0,
  },
  checkpoint: {
    next_ordinal: 11843,
    chain_digest: 'b'.repeat(64),
    last_progress_at: '2026-08-21T15:20:00Z',
  },
  assignment: {
    assignment_id: null,
    credit_start_ordinal: null,
    credit_end_ordinal: null,
    credit_committed_through: 11843,
    granted_at: null,
    expires_at: null,
    accepted_at: null,
    heartbeat_at: null,
  },
  eta: {
    low_seconds: null,
    high_seconds: null,
    confidence: 'WAITING',
    unavailable_reason: sourceExceptionAssurance.open
      ? 'SOURCE_QUARANTINE_REQUIRES_REVIEW'
      : 'ORACLE_VERIFYING',
  },
  recovery: {
    operation_id: 'multan-operation',
    source_epoch: 1,
    source_epoch_id: 'multan-epoch',
    divergence: null,
  },
  capture_certificate: { quarantined: 1 },
  oracle_certificate: null,
  requested_at: '2026-08-21T14:00:00Z',
  started_at: '2026-08-21T14:01:00Z',
  capture_certified_at: '2026-08-21T15:06:22Z',
  oracle_certified_at: null,
  completed_at: null,
  updated_at: '2026-08-21T15:20:00Z',
})

const sourceException: SourceException = {
  id: 1842,
  connector_id: device.connector_id,
  device_id: device.device_id,
  display_name: device.display_name,
  zone_id: device.zone_id,
  terminal_serial: 'RKQ4245100152',
  terminal_generation: 1,
  ordinal: 11842,
  source_kind: 'BASELINE',
  record_size: 40,
  disposition: 'INVALID_TIME',
  error_code: 'IMPLAUSIBLE_TERMINAL_TIME',
  raw_timestamp: 0,
  observed_uid: '0',
  observed_user_id: null,
  raw_record_digest: 'c'.repeat(64),
  evidence_available: true,
  terminal_record_key: 'd'.repeat(64),
  attendance_event_id: null,
  observed_at: '2026-08-21T15:06:20Z',
  review_state: 'OPEN',
  reviewed_at: null,
  reviewed_by: null,
  review_reason: null,
  source_committed_cursor: 11843,
  cursor_advanced: true,
  oracle_action: 'EXCLUDED_FAIL_CLOSED',
  reviews: [],
}

const json = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })

const scheduler = {
  policy: 'BOUNDED_PARALLEL_PER_DEVICE',
  device_concurrency: 6,
  active_scan_jobs: 0,
  waiting_scan_jobs: 0,
  available_scan_slots: 6,
  history_backlog: 6055,
  history_backlog_limit: 20000,
}

// Card headings use the friendly name the dashboard shows (backend friendly_name
// or the mirror of be/device_names.py), never the raw device code.
const nameOf = (code: string) => friendlyNames([code])[0].name

function Harness({
  terminal = device,
  devices,
  revision = 0,
}: { terminal?: Device; devices?: Device[]; revision?: number } = {}) {
  const toast = useToast()
  return (
    <BrowserRouter>
      <ReconciliationView
        devices={devices || [terminal]}
        revision={revision}
        toast={toast}
      />
      {toast.toast && <div role="status" data-testid="toast">{toast.toast.text}</div>}
    </BrowserRouter>
  )
}

const toastText = async (pattern: RegExp) => {
  const node = await screen.findByTestId('toast')
  await waitFor(() => expect(node.textContent).toMatch(pattern))
}

const card = async (code: string) =>
  (await screen.findByRole('heading', { name: nameOf(code) })).closest('article') as HTMLElement

const recordRows = async () => within(await screen.findByRole('list', { name: 'Unreadable records' })).findAllByRole('listitem')

const openRecord = async () => {
  const [row] = await recordRows()
  fireEvent.click(within(row).getByRole('button', { name: /^View record/ }))
  return screen.findByRole('dialog', { name: 'Unreadable record' })
}

const markSeen = async (note: string) => {
  fireEvent.click(await screen.findByRole('button', { name: 'Mark as seen' }))
  fireEvent.change(screen.getByLabelText('Note (why you are marking this)'), { target: { value: note } })
  fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'admin-password' } })
  fireEvent.click(screen.getByRole('button', { name: 'Mark as seen' }))
}

function reconciliationFetch(terminalStatus?: ReconciliationJob['status']) {
  let reviewed = terminalStatus !== undefined
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'https://add.test')
    if (url.pathname === `/api/v1/source-exceptions/${sourceException.id}/review`) {
      reviewed = true
      return json({
        ...sourceException,
        review_state: 'REVIEWED',
        reviewed_at: '2026-08-21T15:30:00Z',
        reviewed_by: 'StateHealthAdmin',
        review_reason: 'Reviewed immutable source timestamp evidence.',
        reviews: [
          {
            review_id: 'review-one',
            state: 'REVIEWED',
            reason: 'Reviewed immutable source timestamp evidence.',
            actor: 'StateHealthAdmin',
            created_at: '2026-08-21T15:30:00Z',
          },
        ],
      })
    }
    if (url.pathname === `/api/v1/source-exceptions/${sourceException.id}`)
      return json(sourceException)
    if (url.pathname === '/api/v1/source-exceptions') {
      const currentAssurance = reviewed
        ? assurance({ reviewed: 1, open: 0, state: 'REVIEWED_EXCLUSIONS' })
        : assurance()
      return json({
        totals: {
          all: 1,
          open: reviewed ? 0 : 1,
          reviewed: reviewed ? 1 : 0,
          invalid_time: 1,
          malformed: 0,
          affected_terminals: 1,
        },
        rows: [
          reviewed
            ? { ...sourceException, review_state: 'REVIEWED' }
            : sourceException,
        ],
        next_cursor: null,
        filtered_total: 1,
        scope: url.searchParams.get('job_id')
          ? {
              job_id: job().job_id,
              device_id: device.connector_id,
              terminal_serial: 'RKQ4245100152',
              cutoff_count: 11843,
              source_exception_assurance: currentAssurance,
            }
          : undefined,
      })
    }
    if (url.pathname === '/api/v1/reconciliations') {
      const currentJob = reviewed
        ? job(
            assurance({
              reviewed: 1,
              open: 0,
              state: 'REVIEWED_EXCLUSIONS',
            }),
          )
        : job()
      return json({
        enabled: true,
        scheduler,
        rows: [{ ...currentJob, ...(terminalStatus ? { status: terminalStatus } : {}) }],
        next_cursor: null,
        filtered_total: 1,
        totals: {
          all: 1,
          active: reviewed ? 1 : 0,
          queued_waiting: 0,
          paused: 0,
          attention: reviewed ? 0 : 1,
          completed: 0,
          cancelled: 0,
        },
      })
    }
    if (url.pathname === '/api/v1/attendance-repairs')
      return json({
        preview_enabled: false,
        execution_enabled: false,
        rows: [],
        next_cursor: null,
        totals: { all: 0, active: 0, attention: 0 },
        worker: {
          active_jobs: 0,
          review_items: 0,
          stale_leases: 0,
          oldest_job_age_seconds: 0,
        },
      })
    if (url.pathname === `/api/v1/reconciliations/${job().job_id}`)
      return json(job())
    if (url.pathname === `/api/v1/source-exceptions/${sourceException.id}/reveal`)
      return json({})
    throw new Error(`Unexpected request: ${url.pathname} ${init?.method || 'GET'}`)
  })
}

describe('Reviewed unreadable-record continuation', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/reconciliation')
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it.each([
    ['COMPLETED', 'Done. Some records were not sent.'],
    ['CANCELLED', 'Stopped. Unreadable records are kept.'],
  ] as const)('keeps %s wording distinct from ongoing checks', async (status, label) => {
    vi.stubGlobal('fetch', reconciliationFetch(status))
    render(<Harness />)
    expect(await screen.findByText(label)).toBeTruthy()
    expect(screen.queryByText('All records looked at')).toBeNull()
  })

  it('replaces a pointless Try again with the exact records to look at', async () => {
    const fetchMock = reconciliationFetch()
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)

    const article = await card('ZONE-MULTAN-01')
    expect(within(article).queryByRole('button', { name: /^Try again$/ })).toBeNull()
    expect(within(article).getByText('1 record to look at')).toBeTruthy()
    fireEvent.click(within(article).getByRole('button', { name: 'Look at 1 record' }))
    expect((await screen.findAllByText(/From the recovery of/)).length).toBeGreaterThan(0)
    expect(window.location.search).toContain(`job_id=${job().job_id}`)
    expect(window.location.search).toContain('tab=unreadable')
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([input]) => String(input).includes(`job_id=${job().job_id}`))).toBe(true),
    )
  })

  it('records the final review and says the recovery finishes by itself, with no second approval', async () => {
    window.history.replaceState(null, '', `/reconciliation?tab=source-exceptions&job_id=${job().job_id}&device_id=${device.connector_id}`)
    const fetchMock = reconciliationFetch()
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)

    await openRecord()
    await markSeen('Reviewed immutable source timestamp evidence.')
    await toastText(/finishes by itself when none are left/)
    await waitFor(() => expect(screen.getAllByText('All records looked at').length).toBeGreaterThan(0))
    expect(screen.queryByRole('button', { name: /Authorize exclusions/i })).toBeNull()
    const reviewRequest = fetchMock.mock.calls.find(([input]) => String(input).endsWith(`/source-exceptions/${sourceException.id}/review`))
    expect(reviewRequest?.[1]?.method).toBe('POST')
    expect(String(reviewRequest?.[1]?.body)).not.toContain(job().job_id)
    expect(Object.keys(JSON.parse(String(reviewRequest?.[1]?.body))).sort()).toEqual(['idempotency_key', 'password', 'reason'])
  })

  it('shows records that cannot be read yet without review or Try again, and never 100%', async () => {
    const base = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const response = await base(input, init)
      const body = await response.json()
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === '/api/v1/reconciliations' || path === `/api/v1/reconciliations/${job().job_id}`) {
        const pending = job(assurance({ state: 'INTERPRETATION_REQUIRED', invalid_time: 0, raw_preserved: 1, raw_interpretation_open: 1 }))
        const row = { ...pending, status: 'NEEDS_ATTENTION', phase: 'WAITING_FOR_PROTOCOL', wait_reason: 'SOURCE_INTERPRETATION_REQUIRED',
          progress: { ...pending.progress, quarantined: 0, raw_preserved: 1, oracle_target: 1, oracle_confirmed: 1 } }
        if (path === '/api/v1/reconciliations') body.rows = [row]
        else return json(row)
      }
      return json(body)
    }))
    render(<Harness />)
    expect(await screen.findByText("Some records can't be read yet")).toBeTruthy()
    expect(screen.getByText(/IT must look at them\. Nothing was sent for them\./)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Try again$|^Look at \d/ })).toBeNull()
    expect(screen.getByRole('progressbar', { name: 'Reached the HR system' }).getAttribute('aria-valuenow')).not.toBe('100')
    fireEvent.click(screen.getByRole('button', { name: 'Details' }))
    const sheet = await screen.findByRole('dialog', { name: 'Recovery details' })
    expect(within(sheet).getAllByText(/IT must look at them/).length).toBeGreaterThan(0)
    expect(within(sheet).queryByRole('button', { name: /^Look at/ })).toBeNull()
  })

  it('keeps a record not matched to an employee unsent after it is marked as seen', async () => {
    let reviewed = false
    const base = reconciliationFetch()
    const missingIdentity = () => ({
      ...sourceException, disposition: 'IDENTITY_UNRESOLVED',
      observed_uid: '40', observed_user_id: null,
      oracle_action: 'HELD_IDENTITY_EVIDENCE_REQUIRED',
      review_state: reviewed ? 'REVIEWED' : 'OPEN',
    })
    const pending = () => assurance({
      state: 'IDENTITY_EVIDENCE_REQUIRED', invalid_time: 0,
      identity_unresolved: 1, identity_unresolved_open: 1,
      reviewed: reviewed ? 1 : 0, open: 1,
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === `/api/v1/source-exceptions/${sourceException.id}/review`) {
        reviewed = true
        return json(missingIdentity())
      }
      if (path === `/api/v1/source-exceptions/${sourceException.id}`) return json(missingIdentity())
      const response = await base(input, init)
      const body = await response.json()
      if (path === '/api/v1/reconciliations') {
        body.rows = [{ ...job(pending()), status: 'NEEDS_ATTENTION', phase: 'WAITING_FOR_IDENTITY', wait_reason: 'SOURCE_IDENTITY_EVIDENCE_REQUIRED' }]
      } else if (path === '/api/v1/source-exceptions') {
        body.rows = [missingIdentity()]
        body.totals.identity_unresolved = 1
        if (body.scope) body.scope.source_exception_assurance = pending()
      }
      return json(body)
    }))
    render(<Harness />)
    expect(await screen.findByText("Some check-ins aren't matched to an employee")).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Try again$/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'See records not sent' }))
    const sheet = await openRecord()
    expect(await within(sheet).findByRole('heading', { name: 'Not matched to an employee' })).toBeTruthy()
    expect(within(sheet).getByText(/Marking it as seen will not send it/)).toBeTruthy()
    await markSeen('Investigated missing historical user reference.')
    await toastText(/It stays not matched until the employee is fixed/)
    expect(screen.queryByText('All records looked at')).toBeNull()
    expect(screen.queryByText(/finishes by itself when none are left/)).toBeNull()
  })

  const newestCase = async (row: SourceException) => {
    const base = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === `/api/v1/source-exceptions/${sourceException.id}`) return json(row)
      const response = await base(input, init)
      const body = await response.json()
      if (path === '/api/v1/source-exceptions') body.rows = [{ ...row, slot_receipts: undefined }]
      return json(body)
    }))
    window.history.replaceState(null, '', `/reconciliation?tab=source-exceptions&job_id=${job().job_id}&device_id=${device.connector_id}`)
    render(<Harness />)
    return (await recordRows())[0]
  }

  it('explains an unwritten newest record instead of a clock fault', async () => {
    const row = await newestCase({
      ...sourceException, source_kind: 'TAIL', ordinal: 15379, source_condition: 'TERMINAL_NEWEST_RECORD_UNSETTLED',
      operator_reason: 'The terminal reported this ordinal as its newest record before it had written the record.',
    })
    expect(within(row).getByText('Not a real check-in: the machine was still saving it')).toBeTruthy()
    const sheet = await openRecord()
    expect(within(sheet).getByRole('heading', { name: 'Not a real check-in' })).toBeTruthy()
    // The server's own sentence is kept, under Technical details.
    expect(within(sheet).getByText(/reported this ordinal as its newest record/).closest('[data-technical]')).toBeTruthy()
    expect(within(sheet).queryByRole('heading', { name: 'Impossible time' })).toBeNull()
  })

  it('flags a time corrected from an unwritten newest record for HR', async () => {
    const row = await newestCase({
      ...sourceException, source_kind: 'TAIL', ordinal: 14360, source_condition: 'TERMINAL_NEWEST_RECORD_CORRECTED',
      operator_reason: 'A source time correction was created from this row before ADD recognised it as the terminal\'s unwritten newest record. HR must verify that punch.',
      source_correction_ids: ['b9c8e8a0-6f4f-4a56-9a53-6a0c9f3d2e11'],
    })
    expect(within(row).getByText('Time was corrected. HR should check this check-in.')).toBeTruthy()
    expect(within(row).queryByText(/Not a real check-in/)).toBeNull()
    const sheet = await openRecord()
    expect(within(sheet).getByRole('heading', { name: 'Time was corrected' })).toBeTruthy()
    expect(within(sheet).getByText(/HR should check this check-in/)).toBeTruthy()
    expect(within(sheet).getByText(/HR must verify that punch/).closest('[data-technical]')).toBeTruthy()
  })

  it('shows a latest check-in not received yet and the later reads of its record', async () => {
    const row = await newestCase({
      ...sourceException, source_kind: 'TAIL', ordinal: 15380, disposition: 'IDENTITY_UNRESOLVED',
      error_code: 'TERMINAL_NEWEST_RECORD_UNCONFIRMED', observed_uid: '17', observed_user_id: '44',
      oracle_action: 'HELD_IDENTITY_EVIDENCE_REQUIRED', source_condition: 'TERMINAL_NEWEST_RECORD_UNCONFIRMED',
      operator_reason: 'The terminal returned these bytes from its newest slot. ADD holds them instead of sending a punch that may belong to someone else.',
      slot_receipts: [{ manifest_id: 9001, source_kind: 'TAIL', job_id: null, raw_record_digest: 'e'.repeat(64), disposition: 'EVENT',
        decodes_as_punch: true, punch_received: false, observed_at: '2026-10-21T08:15:00Z' }],
    })
    expect(within(row).getByText('Latest check-in not received yet: waiting for the link box to send it again')).toBeTruthy()
    expect(within(row).getByText('Employee ID 44')).toBeTruthy()
    const sheet = await openRecord()
    expect(within(sheet).getByRole('heading', { name: 'Latest check-in not received yet' })).toBeTruthy()
    const reads = within(sheet).getByRole('region', { name: 'Later reads of this record' })
    expect(within(reads).getByText(/Looks like a check-in the dashboard has not received/)).toBeTruthy()
    expect(within(sheet).getByText(/does not settle the newest record/)).toBeTruthy()
    expect(within(sheet).queryByRole('heading', { name: 'Not matched to an employee' })).toBeNull()
  })

  it('shows newest-record bytes out of time order as a record not sent', async () => {
    const row = await newestCase({
      ...sourceException, source_kind: 'TAIL', ordinal: 15381, disposition: 'IDENTITY_UNRESOLVED',
      error_code: 'TERMINAL_NEWEST_RECORD_UNCONFIRMED', oracle_action: 'EXCLUDED_FAIL_CLOSED',
      source_condition: 'TERMINAL_NEWEST_RECORD_OUT_OF_ORDER',
      operator_reason: 'A complete reconcile read the same bytes again while later records existed, but their time does not lie between the punches before and after it.',
    })
    expect(within(row).getByText('Time is out of order, so it was not sent')).toBeTruthy()
    const sheet = await openRecord()
    expect(within(sheet).getByRole('heading', { name: 'Time is out of order' })).toBeTruthy()
    expect(within(sheet).getByText(/does not lie between the punches before and after it/)).toBeTruthy()
    expect(within(sheet).queryByRole('heading', { name: 'Not matched to an employee' })).toBeNull()
  })

  it('waits for a latest check-in the dashboard has not received, with no Try again', async () => {
    const pending = assurance({ state: 'NEWEST_PUNCH_UNRECEIVED', reviewed: 1, open: 1, newest_punch_unreceived: 1 })
    const base = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      const response = await base(input, init)
      const body = await response.json()
      if (path === '/api/v1/reconciliations') {
        body.rows = [{ ...job(pending), status: 'NEEDS_ATTENTION', phase: 'FINAL_ASSURANCE',
          wait_reason: 'SOURCE_NEWEST_PUNCH_UNRECEIVED', error_code: 'SOURCE_NEWEST_PUNCH_UNRECEIVED' }]
      }
      return json(body)
    }))
    render(<Harness />)
    expect(await screen.findByText('Latest check-in not received yet')).toBeTruthy()
    expect(screen.getByText(/latest check-in hasn.t reached us/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Try again$/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'See records not sent' })).toBeTruthy()
  })

  it('offers Stop, never Try again, when the recovery could not finish safely', async () => {
    const base = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      const response = await base(input, init)
      const body = await response.json()
      if (path === '/api/v1/reconciliations') {
        body.rows = [{ ...job(assurance({ total: 0, open: 0, state: 'NONE' })), status: 'NEEDS_ATTENTION', phase: 'FINAL_ASSURANCE',
          wait_reason: 'SOURCE_COVERAGE_REVIEW', error_code: 'SOURCE_COVERAGE_CHAIN_GAP', operator_state: 'REVIEW_REQUIRED' }]
      }
      return json(body)
    }))
    render(<Harness />)
    const article = await card('ZONE-MULTAN-01')
    expect(within(article).getByText(/Couldn't finish safely/)).toBeTruthy()
    expect(within(article).getByRole('button', { name: 'Stop' })).toBeTruthy()
    fireEvent.click(within(article).getByRole('button', { name: /More actions for/ }))
    expect(screen.queryByRole('menuitem', { name: 'Try again' })).toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Stop and hide' })).toBeTruthy()
  })
})


const requestBody = (fetchMock: ReturnType<typeof vi.fn>, suffix: string) => {
  const call = fetchMock.mock.calls.find(([input, init]) =>
    new URL(String(input), 'https://add.test').pathname.endsWith(suffix) && (init as RequestInit | undefined)?.method === 'POST')
  return call ? JSON.parse(String((call[1] as RequestInit).body)) : undefined
}

const zktPreflight = {
  eligible: true, ready_now: true, hard_blockers: [], waitable_blockers: [],
  terminal: { serial: 'RKQ4245100152', connection_state: 'ONLINE', range_resume_verified: true },
  coverage: null,
}

async function selectTerminal() {
  fireEvent.click(screen.getByRole('button', { name: 'Choose device' }))
  fireEvent.click(within(await screen.findByRole('listbox', { name: 'Devices' })).getAllByRole('option')[0])
  const start = await screen.findByRole('button', { name: 'Start recovery' }) as HTMLButtonElement
  await waitFor(() => expect(start.disabled).toBe(false))
  return start
}

describe('Hikvision: what to read', () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })
  it.each([['ALL_RECORDS', 'Everything on the machine'], ['ACTIVE_USERS', 'Current staff only']] as const)('starts %s in one click with no reason, phrase or password', async (scope, label) => {
    window.history.replaceState(null, '', '/reconciliation')
    const hik = { ...device, firmware_family: 'hikvision' as const }
    const fallback = reconciliationFetch()
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === `/api/v1/devices/${device.connector_id}`) return json(hik)
      if (path.endsWith('/reconciliations/preflight')) return json({
        eligible: true, ready_now: true, hard_blockers: [], waitable_blockers: [],
        terminal: null, coverage: null, source_protocol: 'hikvision-isapi-v1',
      })
      if (path.endsWith('/reconciliations/full-history')) return json(job())
      return fallback(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness terminal={hik} />)
    const start = await selectTerminal()
    const group = screen.getByRole('radiogroup', { name: 'What to read' })
    expect((within(group).getByLabelText('Everything on the machine') as HTMLInputElement).checked).toBe(true)
    fireEvent.click(within(group).getByLabelText(label))
    fireEvent.click(start)
    await waitFor(() => expect(requestBody(fetchMock, '/reconciliations/full-history')).toBeTruthy())
    const body = requestBody(fetchMock, '/reconciliations/full-history')
    expect(body.scope).toBe(scope)
    expect(Object.keys(body).sort()).toEqual(['idempotency_key', 'scope'])
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it.each([true, false])('offers the newest check-ins only when there is a saved starting point (%s)', async (eligible) => {
    window.history.replaceState(null, '', '/reconciliation')
    const hik = { ...device, firmware_family: 'hikvision' as const }
    const fallback = reconciliationFetch()
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === `/api/v1/devices/${device.connector_id}`) return json(hik)
      if (path.endsWith('/reconciliations/preflight')) return json({
        eligible: true, ready_now: true, hard_blockers: [], waitable_blockers: [],
        terminal: null, coverage: null, source_protocol: 'hikvision-isapi-v1',
        recent_tail: eligible
          ? { eligible: true, ready_now: true, anchor_serial: 597801, begin_serial: 597782, earlier_breaks: [512000], reason: null }
          : { eligible: false, anchor_serial: null, begin_serial: null,
              reason: 'ADD holds no checkpointed live-poll record for this terminal; use a full terminal audit instead.' },
      })
      if (path.endsWith('/reconciliations/full-history')) return json(job())
      return fallback(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness terminal={hik} />)
    const start = await selectTerminal()
    fireEvent.click(screen.getByLabelText('Only the newest check-ins (catch up)'))
    expect(await screen.findByText(eligible
      ? /Starts from record 597,782.*older gaps after record 512,000/
      : /We have no saved starting point for this machine/)).toBeTruthy()
    // The server's own sentence stays under Why? › Technical details.
    if (!eligible) expect(screen.getByText(/holds no checkpointed live-poll record/).closest('[data-technical]')).toBeTruthy()
    expect(start.disabled).toBe(!eligible)
    if (!eligible) return
    fireEvent.click(start)
    await waitFor(() => expect(requestBody(fetchMock, '/reconciliations/full-history')).toMatchObject({ scope: 'RECENT_TAIL' }))
  })
})

describe('One-click recovery', () => {
  beforeEach(() => window.history.replaceState(null, '', '/reconciliation'))
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  const fetchWith = (
    overrides: (path: string, init: RequestInit | undefined, url: URL) => Response | Promise<Response> | undefined,
    status?: ReconciliationJob['status'],
  ) => {
    const fallback = reconciliationFetch(status)
    return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://add.test')
      const path = url.pathname
      const custom = await overrides(path, init, url)
      if (custom) return custom
      if (path === `/api/v1/devices/${device.connector_id}`) return json(device)
      if (path.endsWith('/reconciliations/preflight')) return json(zktPreflight)
      if (path === '/api/v2/attendance-recovery/summary')
        return json({ delivery: { safe_retryable: 0, identity_held: 0, permanent_review: 0 } })
      return fallback(input, init)
    })
  }

  it('starts a ZKTeco recovery with one click and sends no password or reason', async () => {
    const fetchMock = fetchWith((path) =>
      path.endsWith('/reconciliations/full-history') ? json(job()) : undefined)
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)
    expect(screen.queryByText(/Jobs needing attention first/)).toBeNull()
    expect(screen.getByText(/Newest first/)).toBeTruthy()
    const start = await selectTerminal()
    expect(screen.getByText('Ready. Starts now.')).toBeTruthy()
    expect(screen.queryByRole('radiogroup', { name: 'What to read' })).toBeNull()
    fireEvent.click(start)
    await toastText(new RegExp(`Recovery started for ${nameOf('ZONE-MULTAN-01')} \\(Health\\)`))
    expect(Object.keys(requestBody(fetchMock, '/reconciliations/full-history')).sort()).toEqual(['idempotency_key', 'scope'])
    expect(screen.queryByLabelText('Your password')).toBeNull()
    expect(screen.queryByLabelText(/^Note/)).toBeNull()
  })

  it('shows "Show it" instead of a red blocker when a recovery is already running', async () => {
    const fetchMock = fetchWith((path) => path.endsWith('/reconciliations/preflight') ? json({
      ...zktPreflight, eligible: false, ready_now: false,
      hard_blockers: [{ code: 'RECONCILIATION_ACTIVE', message: `Device already has active reconciliation ${job().job_id}.` }],
    }) : undefined)
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Choose device' }))
    fireEvent.click(within(await screen.findByRole('listbox', { name: 'Devices' })).getAllByRole('option')[0])
    expect(await screen.findByText("Can't start: a recovery is already running for this device.")).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Start recovery' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Show it' })).toBeTruthy()
  })

  it('stops with one click and hides a finished recovery without a reload jump', async () => {
    let archived = false
    const running = { ...job(assurance({ total: 0, open: 0, state: 'NONE' })), status: 'RUNNING' as const }
    const finished = { ...job(assurance({ total: 0, open: 0, state: 'NONE' })), job_id: 'finished-job', status: 'COMPLETED' as const,
      connector: { ...job().connector!, display_name: 'Finished terminal' } }
    const fetchMock = fetchWith((path, init) => {
      if (path === `/api/v1/reconciliations/${running.job_id}/cancel`) return json({ ...running, status: 'CANCELLED' })
      if (path === '/api/v1/reconciliations/finished-job/archive') {
        archived = true
        return json({ ...finished, archived_at: '2026-10-23T10:00:00Z' })
      }
      if (path === '/api/v1/reconciliations' && (!init?.method || init.method === 'GET'))
        return json({ enabled: true, scheduler, rows: archived ? [running] : [running, finished], next_cursor: null,
          filtered_total: archived ? 1 : 2, totals: { all: 2, active: 1, queued_waiting: 0, paused: 0, attention: 0, completed: 1, cancelled: 0 } })
      return undefined
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)
    const finishedCard = await card('Finished terminal')
    const runningCard = await card('ZONE-MULTAN-01')
    expect(within(finishedCard).queryByRole('button', { name: 'Stop' })).toBeNull()
    fireEvent.click(within(runningCard).getByRole('button', { name: /More actions for/ }))
    expect(screen.getByRole('menuitem', { name: 'Stop and hide' })).toBeTruthy()
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })

    fireEvent.click(within(runningCard).getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(requestBody(fetchMock, `/${running.job_id}/cancel`)).toBeTruthy())
    expect(Object.keys(requestBody(fetchMock, `/${running.job_id}/cancel`))).toEqual(['idempotency_key'])
    expect(screen.queryByRole('dialog')).toBeNull()

    fireEvent.click(within(finishedCard).getByRole('button', { name: 'Hide' }))
    await waitFor(() => expect(screen.queryByRole('heading', { name: nameOf('Finished terminal') })).toBeNull())
    expect(screen.getByRole('heading', { name: nameOf('ZONE-MULTAN-01') })).toBeTruthy()
    await toastText(/Hidden from the list\./)
  })

  it('keeps busy state per card: another card stays usable while one pauses', async () => {
    let release: () => void = () => undefined
    const first = { ...job(assurance({ total: 0, open: 0, state: 'NONE' })), status: 'RUNNING' as const, operator_state: 'CAPTURING_SOURCE' }
    const second = { ...first, job_id: 'second-job', connector: { ...job().connector!, display_name: 'ZONE-LAHORE-01' } }
    const fetchMock = fetchWith((path, init) => {
      if (path === `/api/v1/reconciliations/${first.job_id}/pause`) return new Promise<Response>((done) => { release = () => done(json({ ...first, status: 'PAUSED' })) })
      if (path === '/api/v1/reconciliations' && (!init?.method || init.method === 'GET'))
        return json({ enabled: true, scheduler, rows: [first, second], next_cursor: null, filtered_total: 2,
          totals: { all: 2, active: 2, queued_waiting: 0, paused: 0, attention: 0, completed: 0, cancelled: 0 } })
      return undefined
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)
    const cardA = await card('ZONE-MULTAN-01')
    const cardB = await card('ZONE-LAHORE-01')
    fireEvent.click(within(cardA).getByRole('button', { name: 'Pause' }))
    expect(await within(cardA).findByText('Pausing…')).toBeTruthy()
    expect((within(cardB).getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false)
    expect((within(cardA).getByRole('button', { name: 'Stop' }) as HTMLButtonElement).disabled).toBe(true)
    release()
    await toastText(/Paused at/)
  })

  it('recovers several devices with one click and summarises the outcome', async () => {
    const second: Device = { ...device, connector_id: 'lahore-connector', device_id: '18', display_name: 'ZONE-LAHORE-01',
      zone_id: 'ZONE-LAHORE', zone_name: 'Lahore' }
    const spare: Device = { ...device, connector_id: 'spare-connector', device_id: '19', display_name: 'ZONE-QUETTA-01', zone_id: 'ZONE-QUETTA', is_spare: true }
    const fetchMock = fetchWith((path) => path === '/api/v1/reconciliations/bulk' ? json({
      created: 1,
      results: [
        { connector_id: device.connector_id, display_name: device.display_name, zone_id: device.zone_id,
          outcome: 'CREATED', job_id: 'new-job', status: 'QUEUED', message: null },
        { connector_id: second.connector_id, display_name: second.display_name, zone_id: second.zone_id,
          outcome: 'BLOCKED', job_id: null, status: null, message: 'Zone Lite 2.3.0 or newer is required.' },
      ],
    }) : undefined)
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness devices={[device, second, spare]} />)
    fireEvent.click(screen.getByRole('button', { name: 'Recover several devices' }))
    const sheet = await screen.findByRole('dialog', { name: 'Recover several devices' })
    expect(within(sheet).getByText(nameOf('ZONE-LAHORE-01'))).toBeTruthy()
    // Spares are left out unless asked for.
    expect(within(sheet).queryByText(nameOf('ZONE-QUETTA-01'))).toBeNull()
    fireEvent.click(within(sheet).getByLabelText(/Choose all Health \(2\)/))
    fireEvent.click(within(sheet).getByRole('button', { name: /Start 2 recoveries/ }))
    expect(await within(sheet).findByText(/1 started · 0 already running · 1 couldn't start/)).toBeTruthy()
    expect(within(sheet).getByText("Couldn't start: the link box needs a software update first.")).toBeTruthy()
    const body = requestBody(fetchMock, '/reconciliations/bulk')
    expect(body.connector_ids.sort()).toEqual([device.connector_id, second.connector_id].sort())
    expect(body.scope).toBe('ALL_RECORDS')
    expect(body).not.toHaveProperty('password')
    expectPlainText(sheet)
  })

  it('clears history with one click and reports the counts in a banner', async () => {
    let cleared = false
    const fetchMock = fetchWith((path, init) => {
      if (path === '/api/v1/reconciliations/clear-history') {
        cleared = true
        return json({ cancelled: 1, deleted: 3, archived: 2, kept_reasons: { SOURCE_COVERAGE: 2 }, busy: 1 })
      }
      if (path === '/api/v1/reconciliations' && cleared && (!init?.method || init.method === 'GET'))
        return json({ enabled: true, scheduler, rows: [], next_cursor: null, filtered_total: 0,
          totals: { all: 0, active: 0, queued_waiting: 0, paused: 0, attention: 0, completed: 0, cancelled: 0 } })
      return undefined
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)
    await card('ZONE-MULTAN-01')
    expect(screen.getByText('Stops running recoveries and removes finished ones from this list. Check-ins are not changed.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Clear history' }))
    expect(await screen.findByText(/History cleared: 1 stopped, 3 removed, 2 hidden\./)).toBeTruthy()
    expect(screen.getByText(/1 was busy\. Press Clear history again/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Clear the rest' })).toBeTruthy()
    expect(Object.keys(requestBody(fetchMock, '/clear-history'))).toEqual(['idempotency_key'])
    await waitFor(() => expect(screen.queryByRole('heading', { name: nameOf('ZONE-MULTAN-01') })).toBeNull())
  })

  it('keeps the readiness answer and loaded older recoveries in place when a revision arrives', async () => {
    const page = (start: number, count: number) => Array.from({ length: count }, (_, index) => ({
      ...job(assurance({ total: 0, open: 0, state: 'NONE' })), status: 'COMPLETED' as const,
      job_id: `job-${start + index}`, friendly_name: `Terminal ${start + index}`, division: 'health',
    }))
    const listCalls: URLSearchParams[] = []
    let preflightCalls = 0
    const fetchMock = fetchWith((path, init, url) => {
      if (path.endsWith('/reconciliations/preflight')) {
        preflightCalls += 1
        return json(zktPreflight)
      }
      if (path !== '/api/v1/reconciliations' || (init?.method && init.method !== 'GET')) return undefined
      const params = url.searchParams
      listCalls.push(params)
      const totals = { all: 45, active: 0, queued_waiting: 0, paused: 0, attention: 0, completed: 45, cancelled: 0 }
      if (params.get('cursor')) return json({ enabled: true, scheduler, rows: page(40, 5), next_cursor: null, filtered_total: 45, totals })
      const limit = Number(params.get('limit'))
      return json({ enabled: true, scheduler, rows: page(0, Math.min(limit, 45)), next_cursor: limit < 45 ? 900 : null,
        filtered_total: 45, totals })
    })
    vi.stubGlobal('fetch', fetchMock)
    const view = render(<Harness revision={0} />)
    await screen.findByRole('heading', { name: 'Terminal 0' })
    expect(listCalls).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Show older recoveries' }))
    await screen.findByRole('heading', { name: 'Terminal 44' })
    await selectTerminal()
    fireEvent.click(screen.getByText('Why?'))
    expect(screen.getByText('Link box')).toBeTruthy()
    const before = preflightCalls

    view.rerender(<Harness revision={1} />)
    // The answer never blanks while the refresh runs.
    expect(screen.getByText('Link box')).toBeTruthy()
    await waitFor(() => expect(preflightCalls).toBeGreaterThan(before))
    await waitFor(() => expect(listCalls.at(-1)?.get('limit')).toBe('45'))
    expect(listCalls.at(-1)?.get('cursor')).toBeNull()
    expect(screen.getByText('Link box')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Terminal 44' })).toBeTruthy()
    expect(screen.getAllByRole('heading', { name: /^Terminal \d+$/ })).toHaveLength(45)
    // 45 rendered cards plus a refresh: about 5 s on a busy machine, so the 5 s default flakes.
  }, 20_000)
})

describe('Page frame', () => {
  beforeEach(() => window.history.replaceState(null, '', '/recover'))
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  it('never shows a fake 0 while loading, then action-only counts', async () => {
    let release: () => void = () => undefined
    let held = false
    const fallback = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === '/api/v1/reconciliations') {
        held = true
        await new Promise<void>((done) => { release = done })
      }
      return fallback(input, init)
    }))
    render(<Harness />)
    expect(screen.getByRole('heading', { level: 1, name: 'Recover attendance' })).toBeTruthy()
    expect(screen.getByText('Loading recoveries', { selector: '.sr-only' })).toBeTruthy()
    expect(screen.queryByText(/\b0 running\b/)).toBeNull()
    expect(screen.getByRole('tab', { name: /Recoveries/ }).textContent).toContain('—')
    await waitFor(() => expect(screen.getByRole('tab', { name: /Unreadable records/ }).textContent).toMatch(/1$/))
    await waitFor(() => expect(held).toBe(true))
    release()
    expect(await screen.findByText(/1 recovery is waiting for you/)).toBeTruthy()
    await waitFor(() => expect(screen.getByRole('tab', { name: /Recoveries/ }).textContent).toMatch(/Recoveries\s*1$/))
    expect(screen.getByRole('tab', { name: 'Not in HR system yet' })).toBeTruthy()
  })

  it('says it could not load recoveries, with Try again', async () => {
    const fallback = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === '/api/v1/reconciliations') return new Response(JSON.stringify({ detail: 'boom' }), { status: 500, headers: { 'Content-Type': 'application/json' } })
      return fallback(input, init)
    }))
    render(<Harness />)
    expect((await screen.findAllByText("Couldn't load recoveries.")).length).toBeGreaterThan(0)
    expect(screen.getAllByRole('button', { name: 'Try again' }).length).toBeGreaterThan(0)
    expect(screen.getByRole('tab', { name: /Recoveries/ }).textContent).not.toMatch(/\d/)
  })

  it('uses plain words on every tab', async () => {
    vi.stubGlobal('fetch', reconciliationFetch())
    const view = render(<Harness />)
    await card('ZONE-MULTAN-01')
    expectPlainText(view.container)
    fireEvent.click(screen.getByRole('tab', { name: /Unreadable records/ }))
    await recordRows()
    expect(window.location.search).toBe('?tab=unreadable')
    expectPlainText(view.container)
  })

  it('applies record filters at once and keeps them in the URL', async () => {
    window.history.replaceState(null, '', '/recover?tab=unreadable')
    const fetchMock = reconciliationFetch()
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)
    await recordRows()
    const lastExceptionCall = () => new URL(String(fetchMock.mock.calls.filter(([input]) => new URL(String(input), 'https://add.test').pathname === '/api/v1/source-exceptions').at(-1)?.[0]), 'https://add.test')
    expect(lastExceptionCall().searchParams.get('review_state')).toBe('OPEN')
    fireEvent.click(screen.getByRole('button', { name: 'Seen' }))
    await waitFor(() => expect(window.location.search).toContain('review=reviewed'))
    await waitFor(() => expect(lastExceptionCall().searchParams.get('review_state')).toBe('REVIEWED'))
    fireEvent.click(screen.getByRole('button', { name: 'Impossible time' }))
    await waitFor(() => expect(window.location.search).toContain('problem=time'))
    await waitFor(() => expect(lastExceptionCall().searchParams.get('disposition')).toBe('INVALID_TIME'))
    fireEvent.click(screen.getByRole('button', { name: /Remove filter: Problem: Impossible time/ }))
    await waitFor(() => expect(window.location.search).not.toContain('problem='))
  })

  it('offers the next record after marking one as seen', async () => {
    const second = { ...sourceException, id: 1843, ordinal: 11843 }
    const base = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      const response = await base(input, init)
      if (path !== '/api/v1/source-exceptions') return response
      const body = await response.json()
      body.rows = [body.rows[0], second]
      return json(body)
    }))
    window.history.replaceState(null, '', '/recover?tab=unreadable')
    render(<Harness />)
    await waitFor(async () => expect((await recordRows()).length).toBe(2))
    const rows = await recordRows()
    fireEvent.click(within(rows[0]).getByRole('button', { name: /^View record/ }))
    await screen.findByRole('dialog', { name: 'Unreadable record' })
    await markSeen('Checked the impossible time with HR.')
    expect(await screen.findByRole('button', { name: 'Next record →' })).toBeTruthy()
  })
})

describe('Not in HR system yet tab', () => {
  beforeEach(() => window.history.replaceState(null, '', '/reconciliation'))
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  it('has no count and never reads the heavy summary while Recoveries is open', async () => {
    let summaryCalls = 0
    const listCalls: string[] = []
    const fallback = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === '/api/v2/attendance-recovery/summary') {
        summaryCalls += 1
        return json({ delivery: { safe_retryable: 4, identity_held: 2, permanent_review: 1 } })
      }
      if (path === '/api/v1/reconciliations' && (!init?.method || init.method === 'GET')) listCalls.push(path)
      return fallback(input, init)
    }))
    const view = render(<Harness revision={0} />)
    expect(await screen.findByRole('tab', { name: 'Not in HR system yet' })).toBeTruthy()
    await waitFor(() => expect(listCalls.length).toBeGreaterThan(0))
    const before = listCalls.length

    view.rerender(<Harness revision={1} />)
    // The list still refreshes for the revision; the heavy summary is not read at all.
    await waitFor(() => expect(listCalls.length).toBeGreaterThan(before))
    expect(summaryCalls).toBe(0)
  })
})

describe('Hikvision catch-up card', () => {
  beforeEach(() => window.history.replaceState(null, '', '/reconciliation'))
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  it('counts only records after the starting point as new', async () => {
    const fallback = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const response = await fallback(input, init)
      if (new URL(String(input), 'https://add.test').pathname !== '/api/v1/reconciliations') return response
      const body = await response.json()
      return json({
        ...body,
        rows: [{
          ...body.rows[0],
          mode: 'HIKVISION_SERIAL_HISTORY',
          scope: 'RECENT_TAIL',
          recent_tail: {
            anchor_serial: 597801, begin_serial: 597782, boundary_serial: 597782, earlier_breaks: [],
            last_serial: 597801, anchor_returned: true, pages: 2, records: 0, overlap_records: 20,
            fact_conflicts: 0, confirmed_conflicts: 0, continuity_conflicts: 0, caught_up: true,
            caught_up_at: '2026-10-22T15:00:00Z', last_page_at: '2026-10-22T15:00:00Z', coverage_certified: false,
          },
        }],
      })
    }))
    render(<Harness />)
    expect(await screen.findByText(/Newest check-ins · 0 new since record 597801/)).toBeTruthy()
    expect(screen.getByText(/0 new records after serial 597801 · 20 held records re-read/).closest('[data-technical]')).toBeTruthy()
  })
})
