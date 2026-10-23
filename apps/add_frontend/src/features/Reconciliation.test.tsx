import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useToast } from '../App'
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

function Harness({
  terminal = device,
  devices,
  revision = 0,
}: { terminal?: Device; devices?: Device[]; revision?: number } = {}) {
  const toast = useToast()
  return (
    <>
      <ReconciliationView
        devices={devices || [terminal]}
        revision={revision}
        toast={toast}
      />
      {toast.toast && <div role="status">{toast.toast.text}</div>}
    </>
  )
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

describe('Reviewed source-exception continuation', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/reconciliation')
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it.each([
    ['COMPLETED', 'Completed with reviewed exclusions'],
    ['CANCELLED', 'Cancelled — source exceptions remain preserved'],
  ] as const)('keeps %s wording distinct from ongoing assurance', async (status, label) => {
    vi.stubGlobal('fetch', reconciliationFetch(status))
    render(<Harness />)
    expect(await screen.findByText(label)).toBeTruthy()
    expect(screen.queryByText('Reviewed exclusions — assurance continuing')).toBeNull()
  })

  it('replaces ineffective retry with an exact certified-cohort review path', async () => {
    const fetchMock = reconciliationFetch()
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)

    const card = await screen.findByRole('heading', { name: 'ZONE-MULTAN-01' })
    const article = card.closest('article') as HTMLElement
    expect(within(article).queryByRole('button', { name: /^Retry$/ })).toBeNull()
    expect(
      within(article).getByRole('button', { name: /Review 1 exception/i }),
    ).toBeTruthy()
    expect(within(article).getByText(/1 source review remaining/i)).toBeTruthy()

    fireEvent.click(
      within(article).getByRole('button', { name: /Review 1 exception/i }),
    )
    expect(
      await screen.findByText(/Certified job cohort.*cutoff 11,843/i),
    ).toBeTruthy()
    expect(window.location.search).toContain(`job_id=${job().job_id}`)
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([input]) =>
          String(input).includes(`job_id=${job().job_id}`),
        ),
      ).toBe(true),
    )
  })

  it('records the final review and shows automatic continuation without a second approval', async () => {
    window.history.replaceState(
      null,
      '',
      `/reconciliation?tab=source-exceptions&job_id=${job().job_id}&device_id=${device.connector_id}`,
    )
    const fetchMock = reconciliationFetch()
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)

    const row = await screen.findByRole('listitem')
    fireEvent.click(within(row).getByRole('button', { name: 'Inspect' }))
    await screen.findByRole('heading', { name: 'Terminal source exception' })
    fireEvent.change(screen.getByLabelText('Audited reason'), {
      target: { value: 'Reviewed immutable source timestamp evidence.' },
    })
    fireEvent.change(screen.getByLabelText('Administrator password'), {
      target: { value: 'admin-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Mark reviewed/i }))

    expect(
      await screen.findByText(/assurance resumes automatically from its existing checkpoint/i),
    ).toBeTruthy()
    await waitFor(() =>
      expect(
        screen.getAllByText(/All certified exclusions reviewed/i).length,
      ).toBeGreaterThan(0),
    )
    expect(screen.queryByRole('button', { name: /Authorize exclusions/i })).toBeNull()
    const reviewRequest = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith(`/source-exceptions/${sourceException.id}/review`),
    )
    expect(reviewRequest?.[1]?.method).toBe('POST')
    expect(String(reviewRequest?.[1]?.body)).not.toContain(job().job_id)
  })

  it('shows raw custody as pending interpretation without review or retry shortcuts', async () => {
    const base = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const response = await base(input, init)
      const body = await response.json()
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === '/api/v1/reconciliations' || path === `/api/v1/reconciliations/${job().job_id}`) {
        const pending = job(assurance({ state: 'INTERPRETATION_REQUIRED', invalid_time: 0,
          raw_preserved: 1, raw_interpretation_open: 1 }))
        const row = { ...pending, status: 'NEEDS_ATTENTION', phase: 'WAITING_FOR_PROTOCOL',
          wait_reason: 'SOURCE_INTERPRETATION_REQUIRED',
          progress: { ...pending.progress, quarantined: 0, raw_preserved: 1, oracle_target: 1, oracle_confirmed: 1 } }
        if (path === '/api/v1/reconciliations') body.rows = [row]
        else return json(row)
      }
      return json(body)
    }))
    render(<Harness />)
    expect(await screen.findByText('Source interpretation pending')).toBeTruthy()
    expect(screen.getByText(/raw source records preserved; attendance and Oracle completion remain unproven/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Retry$|^Review \d/ })).toBeNull()
    expect(screen.getByRole('progressbar', { name: 'Oracle assurance progress' }).getAttribute('aria-valuenow')).not.toBe('100')
    fireEvent.click(screen.getByRole('button', { name: 'Inspect evidence' }))
    expect(await screen.findByText(/Raw source bytes are preserved. Qualified interpretation is still required/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Inspect certified exceptions' })).toBeNull()
    expect(screen.queryByText('Reviewed exclusions are preserved')).toBeNull()
  })

  it('keeps missing historical identity held after an audited review', async () => {
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
        body.rows = [{ ...job(pending()), status: 'NEEDS_ATTENTION',
          phase: 'WAITING_FOR_IDENTITY', wait_reason: 'SOURCE_IDENTITY_EVIDENCE_REQUIRED' }]
      } else if (path === '/api/v1/source-exceptions') {
        body.rows = [missingIdentity()]
        body.totals.identity_unresolved = 1
        if (body.scope) body.scope.source_exception_assurance = pending()
      }
      return json(body)
    }))
    render(<Harness />)
    expect(await screen.findByText('Historical identity evidence required')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Retry$/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Inspect held source records' }))
    const row = await screen.findByRole('listitem')
    fireEvent.click(within(row).getByRole('button', { name: 'Inspect' }))
    expect(await screen.findByText('Held for verified historical identity')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Audited reason'), {
      target: { value: 'Investigated missing historical user reference.' },
    })
    fireEvent.change(screen.getByLabelText('Administrator password'), {
      target: { value: 'admin-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Mark reviewed/i }))
    expect(await screen.findByText(/Review recorded. The identity hold remains/)).toBeTruthy()
    expect(screen.queryByText('All certified exclusions reviewed')).toBeNull()
    expect(screen.queryByText(/assurance resumes automatically from its existing checkpoint/i)).toBeNull()
  })

  it('explains an unwritten newest terminal record instead of a clock fault', async () => {
    const unsettled: SourceException = {
      ...sourceException,
      source_kind: 'TAIL',
      ordinal: 15379,
      source_condition: 'TERMINAL_NEWEST_RECORD_UNSETTLED',
      operator_reason:
        'The terminal reported this ordinal as its newest record before it had written the record.',
    }
    const base = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === `/api/v1/source-exceptions/${sourceException.id}`) return json(unsettled)
      const response = await base(input, init)
      const body = await response.json()
      if (path === '/api/v1/source-exceptions') body.rows = [unsettled]
      return json(body)
    }))
    window.history.replaceState(
      null,
      '',
      `/reconciliation?tab=source-exceptions&job_id=${job().job_id}&device_id=${device.connector_id}`,
    )
    render(<Harness />)

    const row = await screen.findByRole('listitem')
    expect(within(row).getByText(/Newest record read before the terminal wrote it/)).toBeTruthy()
    fireEvent.click(within(row).getByRole('button', { name: 'Inspect' }))
    expect(
      await screen.findByRole('heading', { name: 'Terminal newest record not yet written' }),
    ).toBeTruthy()
    expect(screen.getByText(/reported this ordinal as its newest record/)).toBeTruthy()
    expect(screen.queryByText('Excluded from attendance and Oracle')).toBeNull()
  })

  it('flags a source correction made from an unwritten newest record for HR', async () => {
    const corrected: SourceException = {
      ...sourceException,
      source_kind: 'TAIL',
      ordinal: 14360,
      source_condition: 'TERMINAL_NEWEST_RECORD_CORRECTED',
      operator_reason:
        'A source time correction was created from this row before ADD recognised it as the terminal\'s unwritten newest record. HR must verify that punch.',
      source_correction_ids: ['b9c8e8a0-6f4f-4a56-9a53-6a0c9f3d2e11'],
    }
    const base = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === `/api/v1/source-exceptions/${sourceException.id}`) return json(corrected)
      const response = await base(input, init)
      const body = await response.json()
      if (path === '/api/v1/source-exceptions') body.rows = [corrected]
      return json(body)
    }))
    window.history.replaceState(
      null,
      '',
      `/reconciliation?tab=source-exceptions&job_id=${job().job_id}&device_id=${device.connector_id}`,
    )
    render(<Harness />)

    const row = await screen.findByRole('listitem')
    expect(within(row).getByText(/Corrected from an unwritten newest record; HR must verify/)).toBeTruthy()
    expect(within(row).queryByText(/Newest record read before the terminal wrote it/)).toBeNull()
    fireEvent.click(within(row).getByRole('button', { name: 'Inspect' }))
    expect(
      await screen.findByRole('heading', { name: 'Corrected from an unwritten newest record' }),
    ).toBeTruthy()
    expect(screen.getByText(/HR must verify that punch/)).toBeTruthy()
    expect(screen.queryByText('Terminal newest record not yet written')).toBeNull()
  })

  it('shows a held newest-record punch and the later reads of its slot', async () => {
    const held: SourceException = {
      ...sourceException,
      source_kind: 'TAIL',
      ordinal: 15380,
      disposition: 'IDENTITY_UNRESOLVED',
      error_code: 'TERMINAL_NEWEST_RECORD_UNCONFIRMED',
      observed_uid: '17',
      observed_user_id: '44',
      oracle_action: 'HELD_IDENTITY_EVIDENCE_REQUIRED',
      source_condition: 'TERMINAL_NEWEST_RECORD_UNCONFIRMED',
      operator_reason:
        'The terminal returned these bytes from its newest slot. ADD holds them instead of sending a punch that may belong to someone else.',
      slot_receipts: [
        {
          manifest_id: 9001,
          source_kind: 'TAIL',
          job_id: null,
          raw_record_digest: 'e'.repeat(64),
          disposition: 'EVENT',
          decodes_as_punch: true,
          punch_received: false,
          observed_at: '2026-10-21T08:15:00Z',
        },
      ],
    }
    const base = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === `/api/v1/source-exceptions/${sourceException.id}`) return json(held)
      const response = await base(input, init)
      const body = await response.json()
      if (path === '/api/v1/source-exceptions') body.rows = [{ ...held, slot_receipts: undefined }]
      return json(body)
    }))
    window.history.replaceState(
      null,
      '',
      `/reconciliation?tab=source-exceptions&job_id=${job().job_id}&device_id=${device.connector_id}`,
    )
    render(<Harness />)

    const row = await screen.findByRole('listitem')
    expect(within(row).getByText(/Newest-record punch held until LIVE capture or a reconcile confirms it/)).toBeTruthy()
    fireEvent.click(within(row).getByRole('button', { name: 'Inspect' }))
    expect(
      await screen.findByRole('heading', { name: 'Held: newest-record punch not confirmed' }),
    ).toBeTruthy()
    expect(screen.getByText(/ADD holds them instead of sending a punch/)).toBeTruthy()
    const reads = screen.getByRole('region', { name: 'Reads of this slot' })
    expect(within(reads).getByText(/Decodes as a punch ADD has not received/)).toBeTruthy()
    expect(screen.getByText(/It does not settle this slot or read its punch/)).toBeTruthy()
    expect(screen.queryByText('Held for verified historical identity')).toBeNull()
  })

  it('shows confirmed newest-record bytes out of time order as a reviewable exclusion', async () => {
    const outOfOrder: SourceException = {
      ...sourceException,
      source_kind: 'TAIL',
      ordinal: 15381,
      disposition: 'IDENTITY_UNRESOLVED',
      error_code: 'TERMINAL_NEWEST_RECORD_UNCONFIRMED',
      oracle_action: 'EXCLUDED_FAIL_CLOSED',
      source_condition: 'TERMINAL_NEWEST_RECORD_OUT_OF_ORDER',
      operator_reason:
        'A complete reconcile read the same bytes again while later records existed, but their time does not lie between the punches before and after it.',
    }
    const base = reconciliationFetch()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'https://add.test').pathname
      if (path === `/api/v1/source-exceptions/${sourceException.id}`) return json(outOfOrder)
      const response = await base(input, init)
      const body = await response.json()
      if (path === '/api/v1/source-exceptions') body.rows = [outOfOrder]
      return json(body)
    }))
    window.history.replaceState(
      null,
      '',
      `/reconciliation?tab=source-exceptions&job_id=${job().job_id}&device_id=${device.connector_id}`,
    )
    render(<Harness />)

    const row = await screen.findByRole('listitem')
    expect(within(row).getByText(/Newest-record bytes out of time order; excluded for review/)).toBeTruthy()
    fireEvent.click(within(row).getByRole('button', { name: 'Inspect' }))
    expect(
      await screen.findByRole('heading', { name: 'Excluded: newest-record bytes out of time order' }),
    ).toBeTruthy()
    expect(screen.getByText(/does not lie between the punches before and after it/)).toBeTruthy()
    expect(screen.queryByText('Held for verified historical identity')).toBeNull()
  })

  it('holds a job whose newest-record slot has a punch ADD has not received', async () => {
    const pending = assurance({
      state: 'NEWEST_PUNCH_UNRECEIVED', reviewed: 1, open: 1, newest_punch_unreceived: 1,
    })
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

    expect(await screen.findByText('Terminal punch not yet received')).toBeTruthy()
    expect(screen.getByText(/1 terminal newest-record slot holds a punch ADD has not received/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Retry$/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Inspect held source records' })).toBeTruthy()
  })


  it('redirects the retired employee-repair deep link to Attendance review', async () => {
    window.history.replaceState(
      null,
      '',
      `/reconciliation?tab=employee-repair&device_id=${device.connector_id}`,
    )
    vi.stubGlobal('fetch', reconciliationFetch())
    render(<Harness />)

    await waitFor(() =>
      expect(`${window.location.pathname}${window.location.search}`).toBe(
        `/attendance?view=needs-review&device_id=${device.connector_id}`,
      ),
    )
    expect(screen.queryByRole('tab', { name: /Employee repair/i })).toBeNull()
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
  fireEvent.click(screen.getByRole('button', { name: 'Select terminal' }))
  fireEvent.click(within(screen.getByRole('listbox')).getAllByRole('option')[0])
  const start = await screen.findByRole('button', { name: 'Reconcile now' }) as HTMLButtonElement
  await waitFor(() => expect(start.disabled).toBe(false))
  return start
}

describe('Hikvision reconciliation scope', () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })
  it.each(['ALL_RECORDS', 'ACTIVE_USERS'] as const)('starts %s in one click with no reason, confirmation or password', async (scope) => {
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
    const select = screen.getByLabelText('History scope') as HTMLSelectElement
    expect(select.value).toBe('ALL_RECORDS')
    if (scope !== 'ALL_RECORDS') fireEvent.change(select, { target: { value: scope } })
    fireEvent.click(start)
    await waitFor(() => expect(requestBody(fetchMock, '/reconciliations/full-history')).toBeTruthy())
    const body = requestBody(fetchMock, '/reconciliations/full-history')
    expect(body.scope).toBe(scope)
    expect(Object.keys(body).sort()).toEqual(['idempotency_key', 'scope'])
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it.each([true, false])('offers recent-tail recovery only when ADD holds a resume point (%s)', async (eligible) => {
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
          ? { eligible: true, ready_now: true, anchor_serial: 597801, begin_serial: 597782,
              earlier_breaks: [512000], reason: null }
          : { eligible: false, anchor_serial: null, begin_serial: null,
              reason: 'ADD holds no checkpointed live-poll record for this terminal; use a full terminal audit instead.' },
      })
      if (path.endsWith('/reconciliations/full-history')) return json(job())
      return fallback(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness terminal={hik} />)
    const start = await selectTerminal()
    fireEvent.change(screen.getByLabelText('History scope'), { target: { value: 'RECENT_TAIL' } })
    expect(await screen.findByText(eligible
      ? /Resumes at terminal serial 597782.*break after serial 512000; only a full history read covers those/
      : /holds no checkpointed live-poll record/)).toBeTruthy()
    expect(start.disabled).toBe(!eligible)
    if (!eligible) return
    fireEvent.click(start)
    await waitFor(() => expect(requestBody(fetchMock, '/reconciliations/full-history')).toMatchObject({ scope: 'RECENT_TAIL' }))
  })
})

describe('One-click reconcile', () => {
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

  it('starts a ZKT reconcile with one click and sends no password or reason', async () => {
    const fetchMock = fetchWith((path) =>
      path.endsWith('/reconciliations/full-history') ? json(job()) : undefined)
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)
    expect(screen.queryByText(/Jobs needing attention first/)).toBeNull()
    expect(screen.getByText(/Newest first/)).toBeTruthy()
    const start = await selectTerminal()
    expect(screen.queryByLabelText('History scope')).toBeNull()
    fireEvent.click(start)
    expect(await screen.findByText(/Full terminal reconciliation queued/)).toBeTruthy()
    expect(Object.keys(requestBody(fetchMock, '/reconciliations/full-history')).sort()).toEqual(['idempotency_key', 'scope'])
    expect(screen.queryByLabelText('Administrator password')).toBeNull()
    expect(screen.queryByLabelText('Audited reason')).toBeNull()
  })

  it('cancels with one click and archives a finished job without a reload jump', async () => {
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
    const finishedCard = (await screen.findByRole('heading', { name: 'Finished terminal' })).closest('article') as HTMLElement
    const runningCard = screen.getByRole('heading', { name: 'ZONE-MULTAN-01' }).closest('article') as HTMLElement
    expect(within(finishedCard).queryByRole('button', { name: 'Cancel' })).toBeNull()
    expect(within(runningCard).getByRole('button', { name: 'Cancel & archive' })).toBeTruthy()

    fireEvent.click(within(runningCard).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(requestBody(fetchMock, `/${running.job_id}/cancel`)).toBeTruthy())
    expect(Object.keys(requestBody(fetchMock, `/${running.job_id}/cancel`))).toEqual(['idempotency_key'])
    expect(screen.queryByRole('dialog')).toBeNull()

    fireEvent.click(within(finishedCard).getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Finished terminal' })).toBeNull())
    expect(screen.getByRole('heading', { name: 'ZONE-MULTAN-01' })).toBeTruthy()
    expect(await screen.findByText('Reconciliation archived.')).toBeTruthy()
  })

  it('reconciles several terminals by zone with one click and summarises the outcome', async () => {
    const second: Device = { ...device, connector_id: 'lahore-connector', device_id: '18', display_name: 'ZONE-LAHORE-01',
      zone_id: 'ZONE-LAHORE', zone_name: 'Lahore' }
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
    render(<Harness devices={[device, second]} />)
    fireEvent.click(screen.getByRole('button', { name: /Bulk reconcile/ }))
    const panel = screen.getByRole('region', { name: 'Reconcile several terminals' })
    expect(within(panel).getByText('Lahore')).toBeTruthy()
    fireEvent.click(within(panel).getByLabelText(/Select all \(2\)/))
    fireEvent.click(within(panel).getByRole('button', { name: 'Reconcile 2 selected' }))
    expect(await within(panel).findByText('1 started')).toBeTruthy()
    expect(within(panel).getByText(/Blocked — Zone Lite 2.3.0 or newer is required/)).toBeTruthy()
    const body = requestBody(fetchMock, '/reconciliations/bulk')
    expect(body.connector_ids.sort()).toEqual([device.connector_id, second.connector_id].sort())
    expect(body.scope).toBe('ALL_RECORDS')
    expect(body).not.toHaveProperty('password')
  })

  it('clears reconcile history with one click and reports the counts', async () => {
    let cleared = false
    const fetchMock = fetchWith((path, init) => {
      if (path === '/api/v1/reconciliations/clear-history') {
        cleared = true
        return json({ cancelled: 1, deleted: 3, archived: 2, kept_reasons: { SOURCE_COVERAGE: 2 } })
      }
      if (path === '/api/v1/reconciliations' && cleared && (!init?.method || init.method === 'GET'))
        return json({ enabled: true, scheduler, rows: [], next_cursor: null, filtered_total: 0,
          totals: { all: 0, active: 0, queued_waiting: 0, paused: 0, attention: 0, completed: 0, cancelled: 0 } })
      return undefined
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)
    await screen.findByRole('heading', { name: 'ZONE-MULTAN-01' })
    fireEvent.click(screen.getByRole('button', { name: /Clear reconcile history/ }))
    expect(await screen.findByRole('status')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toMatch(/1 cancelled, 3 deleted, 2 archived/)
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'ZONE-MULTAN-01' })).toBeNull())
  })

  it('keeps the readiness panel and loaded older jobs in place when a revision arrives', async () => {
    const page = (start: number, count: number) => Array.from({ length: count }, (_, index) => ({
      ...job(assurance({ total: 0, open: 0, state: 'NONE' })), status: 'COMPLETED' as const,
      job_id: `job-${start + index}`, connector: { ...job().connector!, display_name: `Terminal ${start + index}` },
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
    fireEvent.click(screen.getByRole('button', { name: 'Load older jobs' }))
    await screen.findByRole('heading', { name: 'Terminal 44' })
    await selectTerminal()
    expect(screen.getByText('ESP connection')).toBeTruthy()
    const before = preflightCalls

    view.rerender(<Harness revision={1} />)
    // The panel never blanks while the refresh runs.
    expect(screen.getByText('ESP connection')).toBeTruthy()
    await waitFor(() => expect(preflightCalls).toBeGreaterThan(before))
    await waitFor(() => expect(listCalls.at(-1)?.get('limit')).toBe('45'))
    expect(listCalls.at(-1)?.get('cursor')).toBeNull()
    expect(screen.getByText('ESP connection')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Terminal 44' })).toBeTruthy()
    expect(screen.getAllByRole('heading', { name: /^Terminal \d+$/ })).toHaveLength(45)
  })
})

describe('Hikvision recent-tail job card', () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  it('counts only records after the resume point as new', async () => {
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
    expect(await screen.findByText(/0 new records after serial 597801 · 20 held records re-read/)).toBeTruthy()
  })
})
