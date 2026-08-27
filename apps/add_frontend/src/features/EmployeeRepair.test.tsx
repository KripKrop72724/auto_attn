import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useToast } from '../App'
import type { AttendanceRepairJob, Device } from '../types'
import { EmployeeRepair } from './EmployeeRepair'

const testUserKey = 'employee-repair-user-key'

const device = {
  connector_id: 'repair-terminal',
  hardware_id: 'repair-hardware',
  zone_id: 'ZONE-REPAIR',
  zone_name: 'Karachi',
  device_id: '21',
  display_name: 'Karachi repair terminal',
  state: 'ONLINE',
  connected: true,
  firmware_version: '2.4.5',
  onboarding_generation: 1,
  last_onboarded_at: null,
  last_seen_at: '2026-08-27T12:00:00Z',
  current_activity: 'ONLINE',
  last_error_code: null,
  zkt: {
    id: 21,
    serial: 'ADZV211860253',
    expected_serial: 'ADZV211860253',
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
    user_count: 1,
    attendance_count: 1,
    device_time: null,
    device_time_sampled_at: null,
    drift_seconds: 0,
    last_reconcile_at: null,
    next_restart_at: null,
  },
} as Device

const job: AttendanceRepairJob = {
  job_id: '2d496c70-1f3f-4f2b-8d71-5897c6eaeab8',
  connector_id: device.connector_id,
  device_id: device.device_id,
  actor: 'operator',
  status: 'AWAITING_APPROVAL',
  phase: 'PREVIEW_FROZEN',
  date_scope: {
    timezone: 'Asia/Karachi',
    start_utc: null,
    end_utc_exclusive: null,
  },
  request_digest: '1'.repeat(64),
  preview_digest: '2'.repeat(64),
  preview_expires_at: '2026-08-27T12:15:00Z',
  source_dependency_job_id: null,
  totals: {
    employees: 1,
    events: 1,
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
  created_at: '2026-08-27T12:00:00Z',
  approved_at: null,
  started_at: null,
  completed_at: null,
  typed_confirmation: `REPAIR 1 EMPLOYEES / 1 EVENTS ON ${device.device_id} ${'2'.repeat(12)}`,
  downstream_impact: {
    timezone: 'Asia/Karachi',
    calendar_days: 1,
    employee_days: 1,
    before_identity_day_groups: 1,
    desired_identity_day_groups: 1,
    first_date: '2026-08-20',
    last_date: '2026-08-20',
  },
  targets: [
    {
      user_key: testUserKey,
      display_name: 'Ayesha Khan',
      cnic_masked: '*****-****567-1',
      expected_row_version: 7,
      desired_identity_digest: '3'.repeat(64),
      status: 'FROZEN',
      event_count: 1,
      completed_event_count: 0,
      attention_event_count: 0,
    },
  ],
  items: [
    {
      event_uid: '4'.repeat(64),
      user_key: testUserKey,
      event_time: '2026-08-20T03:15:00Z',
      state: 'ORACLE_APPLY',
      oracle_classification: 'MISMATCH',
      outcome: null,
      attempt_count: 0,
      oracle_attempt_count: 0,
      downstream_attempt_count: 0,
      next_attempt_at: null,
      error_code: null,
    },
  ],
  items_next_cursor: null,
}

const json = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })

function Harness() {
  const toast = useToast()
  return <EmployeeRepair devices={[device]} revision={0} toast={toast} />
}

describe('Employee repair workspace', () => {
  beforeEach(() => {
    window.history.replaceState(
      null,
      '',
      `/reconciliation?tab=employee-repair&device_id=${device.connector_id}`,
    )
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('freezes only stable keys and server cohort tokens while execution is dark', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname.endsWith('/attendance-repairs/preflight'))
        return json({
          preview_enabled: true,
          execution_enabled: false,
          eligible: true,
          ready_now: true,
          requires_source_reconciliation: false,
          hard_blockers: [],
          waitable_blockers: [],
          limits: { employees: 500, events: 250000, oracle_batch: 100 },
          source_certificate: { certificate_digest: '5'.repeat(64) },
          terminal: {
            serial: device.zkt?.serial,
            snapshot_complete: true,
            snapshot_stable: true,
            snapshot_revision: 9,
            attendance_count: 1,
          },
          oracle: { available: true, capabilities: { contract_version: '1' } },
          worker: {
            active_jobs: 0,
            review_items: 0,
            stale_leases: 0,
            oldest_job_age_seconds: 0,
          },
        })
      if (url.pathname.endsWith('/users'))
        return json({
          rows: [
            {
              id: 7,
              user_key: testUserKey,
              uid: '7',
              user_id: '1007',
              display_name: 'Ayesha Khan',
              cnic_masked: '*****-****567-1',
              cnic_available: true,
              identity_complete: true,
              identity_conflict_code: null,
              identity_conflict_members: [],
              identity_conflict_resolved: false,
              identity_resolution_id: null,
              shift_worker: false,
              privilege: 0,
              present: true,
              lifecycle_state: 'ACTIVE',
              row_version: 7,
              observed_at: '2026-08-27T12:00:00Z',
              machine_name_preview: null,
              current_command_state: null,
              read_only: false,
            },
          ],
          next_cursor: null,
        })
      if (url.pathname.endsWith('/attendance-repair-candidates/query'))
        return json({
          connector_id: device.connector_id,
          device_id: device.device_id,
          source_current: true,
          source_certificate: { certificate_digest: '5'.repeat(64) },
          date_scope: {
            timezone: 'Asia/Karachi',
            start_utc: null,
            end_utc_exclusive: null,
          },
          targets: [
            {
              user_key: testUserKey,
              row_version: 7,
              display_name: 'Ayesha Khan',
              cnic_masked: '*****-****567-1',
              eligible: true,
              exclusion_code: null,
              cohorts: [
                {
                  cohort_token: '6'.repeat(64),
                  evidence_classification: 'CURRENT_USER_LINEAGE',
                  selectable: true,
                  exclusion_code: null,
                  source_device_user_key: testUserKey,
                  source_uid: '*',
                  source_user_id: '****',
                  first_event_at: '2026-08-20T03:15:00Z',
                  last_event_at: '2026-08-20T03:15:00Z',
                  event_count: 1,
                  membership_digest: '7'.repeat(64),
                  masked_identity: {
                    variants: [
                      {
                        display_name_masked: 'W**** N***',
                        cnic_masked: '*****-****999-1',
                      },
                    ],
                    variant_count: 1,
                    truncated: false,
                  },
                  source_evidence: {
                    terminal_manifest_events: 1,
                    exact_tombstone: false,
                    source_types: ['FULL_HISTORY'],
                  },
                },
              ],
            },
          ],
        })
      if (url.pathname.endsWith('/attendance-repairs/prepare')) return json(job)
      if (url.pathname === '/api/v1/attendance-repairs')
        return json({
          preview_enabled: true,
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
      throw new Error(`Unexpected request: ${url.pathname} ${init?.method || 'GET'}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness />)

    const employee = await screen.findByRole('option', { name: /Ayesha Khan/i })
    expect(document.body.textContent).not.toContain('3520212345671')
    fireEvent.click(employee)
    fireEvent.click(screen.getByRole('button', { name: /Build repair candidates/i }))
    await screen.findByRole('heading', { name: 'Exact source cohorts' })
    fireEvent.click(screen.getByRole('button', { name: 'Freeze immutable preview' }))
    await screen.findByRole('heading', { name: 'Awaiting approval' })

    const prepareCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith('/attendance-repairs/prepare'),
    )
    const body = JSON.parse(String(prepareCall?.[1]?.body))
    expect(body.targets).toEqual([
      {
        user_key: testUserKey,
        expected_row_version: 7,
        all_provable_history: true,
        cohort_tokens: [],
      },
    ])
    expect(String(prepareCall?.[1]?.body)).not.toContain('Ayesha Khan')
    expect(String(prepareCall?.[1]?.body)).not.toContain('3520212345671')
    expect(
      (screen.getByRole('button', {
        name: /Approve repair and resync/i,
      }) as HTMLButtonElement).disabled,
    ).toBe(true)
    await waitFor(() => expect(window.location.search).toContain(`repair_job=${job.job_id}`))
  })
})
