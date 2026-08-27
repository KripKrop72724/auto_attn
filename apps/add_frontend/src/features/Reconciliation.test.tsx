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

function Harness() {
  const toast = useToast()
  return (
    <>
      <ReconciliationView
        devices={[device]}
        revision={0}
        toast={toast}
      />
      {toast.toast && <div role="status">{toast.toast.text}</div>}
    </>
  )
}

function reconciliationFetch() {
  let reviewed = false
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
        rows: [currentJob],
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

  it('supports the employee-repair deep link and keyboard tab navigation', async () => {
    window.history.replaceState(null, '', '/reconciliation?tab=employee-repair')
    vi.stubGlobal('fetch', reconciliationFetch())
    render(<Harness />)

    const repairTab = screen.getByRole('tab', { name: /Employee repair/i })
    expect(repairTab.getAttribute('aria-selected')).toBe('true')
    expect(
      await screen.findByRole('heading', {
        name: 'Repair effective attendance identity',
      }),
    ).toBeTruthy()

    fireEvent.keyDown(repairTab, { key: 'Home' })
    const jobsTab = screen.getByRole('tab', { name: /^Jobs/i })
    expect(jobsTab.getAttribute('aria-selected')).toBe('true')
    expect(document.activeElement).toBe(jobsTab)
    expect(window.location.search).toBe('')
  })
})
