// Shared fixtures and fetch stubs for the App.*.test.tsx files (DLV-2): every
// area's dashboard tests render the whole app against the same stubbed backend.
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, vi } from 'vitest'
import { queryClient } from '../data'
import type { Device, DeviceUser, Overview } from '../types'

export const maskedCnic = '*****-****567-1'
export const fullCnic = '3520212345671'

export const device: Device = {
  connector_id: 'connector-one',
  hardware_id: 'e0:72:a1:d6:f3:28',
  zone_id: 'ZONE-SLICTOWER-3FL',
  zone_name: 'ZONE-SLICTOWER-3FL',
  device_id: '1',
  display_name: 'SLICTOWER · 3rd Floor',
  state: 'ONLINE',
  connected: true,
  firmware_version: '2.1.0',
  onboarding_generation: 2,
  last_onboarded_at: '2026-07-13T12:00:00Z',
  last_seen_at: '2026-07-13T12:01:00Z',
  current_activity: 'Live capture',
  last_error_code: null,
  zkt: {
    id: 1,
    serial: 'ADZV211860253',
    expected_serial: 'ADZV211860253',
    ip_address: '192.168.110.137',
    model: 'MB20/ID',
    platform: 'ZLM60_TFT',
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
    probe_latency_ms: 12,
    certification_state: 'CERTIFIED',
    certification_observations: 2,
    capabilities: { user_write: true, create_user: true, delete_user: true },
    snapshot_complete: true,
    writes_disabled_reason: null,
    user_count: 1,
    attendance_count: 42,
    device_time: '2026-07-13T12:01:00Z',
    device_time_sampled_at: '2026-07-13T12:01:00Z',
    drift_seconds: 0,
    last_reconcile_at: '2026-07-13T12:00:00Z',
    next_restart_at: '2026-07-13T22:00:00Z',
  },
}

export const user: DeviceUser = {
  id: 1,
  user_key: 'test-user-key',
  uid: '7',
  user_id: '1007',
  display_name: 'Ayesha Fatima',
  cnic_masked: maskedCnic,
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
  row_version: 3,
  observed_at: '2026-07-13T12:00:00Z',
  machine_name_preview: 'Ayesha-*****-****567-1',
  current_command_state: null,
  read_only: false,
}

export const conflictedUser: DeviceUser = {
  ...user,
  identity_complete: false,
  identity_conflict_code: 'DUPLICATE_CNIC',
  identity_conflict_members: [{ user_id: '1008', uid: '8' }],
}

export const deliveryAlert = {
  id: 9,
  code: 'ORDS_DELIVERY_FAILED',
  severity: 'WARNING',
  state: 'OPEN',
  message: 'Oracle attendance delivery is retrying; preserved events remain queued.',
  details: {
    failure_category: 'HTTP_503',
    http_status: 503,
    attempt_count: 4,
    response_body: `must-not-render-${fullCnic}`,
  },
  first_seen_at: '2026-07-13T12:00:00Z',
  last_seen_at: '2026-07-13T12:05:00Z',
  acknowledged_at: null,
  resolved_at: null,
}

export class EventSourceStub {
  onmessage: ((event: MessageEvent) => void) | null = null
  constructor(_url: string, _options?: EventSourceInit) {}
  addEventListener() {}
  close() {}
}

export const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })

export const fetchStub = (
  users: DeviceUser[] = [user],
  includeHistorical = false,
  includeEventGroup = false,
  includeActiveEnrichment = false,
  includeCurrentIdentity = false,
  fleetDevices: Device[] = [device],
  overviewOverrides: Partial<Overview> = {},
) =>
  vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path.includes('/api/v1/auth/session')) {
      return response({ username: 'StateHealthAdmin', csrf_token: 'csrf' })
    }
    if (path.includes('/api/v1/overview')) {
      return response({
        total: 1,
        online: 1,
        spares: fleetDevices.filter((row) => row.is_spare).length,
        open_alerts: 1,
        active_leases: 0,
        ords_delivery: {
          backlog: 12,
          pending: 9,
          retrying: 3,
          in_flight: 0,
          blocked_identity: 2,
          quarantined: 1,
          acknowledged: 24,
          acknowledged_add: 24,
          acknowledged_check: 0,
          acknowledged_firmware: 0,
          firmware_unverified: 0,
          membership_reverify: 0,
          oldest_backlog_at: '2026-07-13T12:00:00Z',
          last_attempt_at: '2026-07-13T12:05:00Z',
        },
        ...overviewOverrides,
      })
    }
    if (path.startsWith('/api/v1/firmware/releases')) {
      return response({
        enabled: false,
        hil_enabled: true,
        filtered_total: 1,
        next_cursor: null,
        totals: { all: 1, available: 0, hil_only: 1, revoked: 0 },
        rows: [{
          release_id: 'release-2-2-4',
          version: '2.2.4',
          git_sha: 'f11f9306d7c0f01df19123dda9bea6157a68119d',
          image_sha256: 'a'.repeat(64),
          application_sha256: 'b'.repeat(64),
          image_size: 1_048_576,
          state: 'HIL_ONLY',
          partition_layout: 'ota_0/ota_1',
          signing_key_id: 'zone-lite-production',
          published_at: '2026-07-27T12:00:00Z',
          hil_target_mac: device.hardware_id,
        }],
      })
    }
    if (path.endsWith('/api/v1/firmware/campaigns/bulk') && init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      return response({
        release_id: body.release_id,
        created: body.zone_ids.length,
        results: body.zone_ids.map((zone_id: string) => ({
          zone_id,
          outcome: 'CREATED',
          campaign_id: 'campaign-one',
          status: 'ACTIVE',
          message: null,
          stability_warnings: [],
          eligible: 1,
          fleet_hold: null,
          closed: [],
        })),
      })
    }
    if (path.startsWith('/api/v1/firmware/campaigns') && (init?.method ?? 'GET') === 'GET') {
      return response({
        enabled: false,
        hil_enabled: true,
        rows: [],
        filtered_total: 0,
        next_cursor: null,
        totals: { campaigns: { all: 0 }, deployments: { all: 0 } },
      })
    }
    if (/\/api\/v1\/firmware\/campaigns\/[^/]+\/(pause|resume|cancel)$/.test(path)) {
      return response({ campaign_id: 'campaign-one', status: 'PAUSED' })
    }
    if (path.includes('/logs?')) return response({ rows: [] })
    if (path.includes('/connectivity?')) return response({ rows: [] })
    if (/\/api\/v1\/attendance-quarantine\/\d+\/review$/.test(path) && init?.method === 'POST') {
      return response({
        id: 91,
        review_state: 'REVIEWED',
        reviewed_by: 'StateHealthAdmin',
        review_reason: 'Confirmed malformed source evidence.',
        reviewed_at: '2026-07-13T12:06:00Z',
      })
    }
    if (/\/api\/v1\/attendance-quarantine\/\d+\/reveal$/.test(path) && init?.method === 'POST') {
      return response({
        id: 91,
        payload_digest: 'a'.repeat(64),
        error_code: 'ATTENDANCE_EVENT_STRING_PATTERN_MISMATCH',
        error_path: 'event_uid',
        payload: { terminal_value: 'protected-attendance-evidence' },
      })
    }
    if (path.startsWith('/api/v1/attendance-quarantine')) return response({
      totals: { all: 1, open: 1 },
      filtered_total: 1,
      next_cursor: null,
      rows: [{
        id: 91,
        receipt_id: '11111111-2222-3333-4444-555555555555',
        connector_id: device.connector_id,
        device_id: device.device_id,
        display_name: device.display_name,
        zone_id: device.zone_id,
        batch_id: 'safe-batch-id',
        item_index: 1,
        error_code: 'ATTENDANCE_EVENT_STRING_PATTERN_MISMATCH',
        error_path: 'event_uid',
        payload_digest: 'a'.repeat(64),
        review_state: 'OPEN',
        reviewed_by: null,
        review_reason: null,
        reviewed_at: null,
        observed_at: '2026-07-13T12:05:00Z',
        evidence_available: true,
        handling: 'QUARANTINED_NON_BLOCKING',
      }],
    })
    if (path.startsWith('/api/v1/alerts') && init?.method !== 'POST') return response({
      rows: [{ ...deliveryAlert, device: {
        connector_id: device.connector_id,
        display_name: device.display_name,
        zone_id: device.zone_id,
        hardware_id: device.hardware_id,
      } }],
      next_cursor: null,
      totals: { all: 1, open: 1, acknowledged: 0, resolved: 0 },
    })
    if (path.includes('/alerts')) return response({ rows: [deliveryAlert] })
    if (path.endsWith('/api/v1/devices/connector-one')) return response(device)
    if (path.includes('/api/v1/devices') && !path.includes('/users')) {
      return response({ rows: fleetDevices })
    }
    if (path.includes('/api/v2/devices/connector-one/identity-conflicts')) {
      const hasConflict = users.some((row) => row.identity_conflict_code)
      return response({
        evidence_scope: {
          snapshot_source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT',
          terminal_attendance_count: 42,
          add_attendance_count: 4,
          attendance_coverage_percent: 9.52,
          attendance_is_immutable: true,
          terminal_users_are_unchanged: true,
        },
        raw_duplicate_groups: hasConflict ? 1 : 0,
        resolved_groups: 0,
        unresolved_groups: hasConflict ? 1 : 0,
        groups: hasConflict
          ? [{
              group_token: 'a'.repeat(64),
              cnic_masked: maskedCnic,
              classification: 'POSSIBLE_NAME_VARIANT',
              status: 'UNRESOLVED',
              resolution_id: null,
              resolution_created_at: null,
              resolution_reason: null,
              recommended_action: 'HR_IDENTITY_REVIEW',
              members: [
                {
                  user_key: conflictedUser.user_key,
                  uid: conflictedUser.uid,
                  user_id: conflictedUser.user_id,
                  display_name: conflictedUser.display_name,
                  row_version: conflictedUser.row_version,
                  privilege: conflictedUser.privilege,
                  observed_at: conflictedUser.observed_at,
                  punch_evidence: { captured_count: 4, first_captured_at: null, last_captured_at: null, blocked_identity_count: 1 },
                },
                {
                  user_key: 'other-user-key',
                  uid: '8',
                  user_id: '1008',
                  display_name: 'Ayesha F.',
                  row_version: 2,
                  privilege: 0,
                  observed_at: conflictedUser.observed_at,
                  punch_evidence: { captured_count: 0, first_captured_at: null, last_captured_at: null, blocked_identity_count: 0 },
                },
              ],
            }]
          : [],
      })
    }
    if (
      path.includes('/api/v2/devices/connector-one/historical-identities/resolve-current-identity') &&
      init?.method === 'POST'
    ) {
      return response({ ok: true, repaired_events: 1 }, 201)
    }
    if (
      path.includes('/api/v2/devices/connector-one/historical-identities/resolve-event-group') &&
      init?.method === 'POST'
    ) {
      return response({ ok: true, repaired_events: 2 }, 201)
    }
    if (
      path.includes('/api/v2/devices/connector-one/historical-identities/resolve') &&
      init?.method === 'POST'
    ) {
      return response({ ok: true, repaired_events: 3 }, 201)
    }
    if (path.includes('/api/v2/devices/connector-one/historical-identities')) {
      return response({
        device_serial: device.zkt?.serial,
        snapshot_revision: 8,
        totals: {
          unresolved_events:
            (includeHistorical ? 3 : 0) +
            (includeEventGroup ? 2 : 0) +
            (includeActiveEnrichment ? 1 : 0) +
            (includeCurrentIdentity ? 1 : 0),
          blocked_identity:
            (includeHistorical ? 3 : 0) +
            (includeEventGroup ? 2 : 0) +
            (includeActiveEnrichment ? 1 : 0) +
            (includeCurrentIdentity ? 1 : 0),
          quarantined_identity_reuse: 0,
          attributed_to_deleted_users: includeHistorical ? 3 : 0,
          unassigned_events:
            (includeEventGroup ? 2 : 0) +
            (includeActiveEnrichment ? 1 : 0) +
            (includeCurrentIdentity ? 1 : 0),
          actionable_event_groups:
            includeEventGroup || includeActiveEnrichment || includeCurrentIdentity ? 1 : 0,
          candidate_users: includeHistorical ? 1 : 0,
        },
        rows: includeHistorical
          ? [{
              source_user_key: 'historical-user-key',
              uid: '33',
              user_id: 'CL04196',
              display_name: 'Dr Mehtab',
              row_version: 366,
              observed_at: '2026-07-13T12:00:00Z',
              deleted_at: '2026-07-14T12:00:00Z',
              cnic_available: false,
              identity_conflict_code: null,
              event_count: 3,
              blocked_count: 3,
              quarantined_count: 0,
              first_event_at: '2026-06-01T08:00:00Z',
              last_event_at: '2026-07-01T08:00:00Z',
              resolution_path: 'HR_DIRECTORY_EVIDENCE',
              operator_actionable: true,
            }]
          : [],
        unassigned_groups: includeCurrentIdentity
          ? [{
              source_user_key: null,
              source_kind: 'EVENT_GROUP',
              group_token: 'c'.repeat(64),
              active_user_key: user.user_key,
              active_user_row_version: user.row_version,
              uid: '',
              user_id: '03752',
              display_name: 'Ubaidullah',
              row_version: null,
              observed_at: '2026-07-01T08:00:00Z',
              deleted_at: '2026-07-01T08:00:00Z',
              cnic_available: false,
              identity_conflict_code: null,
              event_count: 1,
              blocked_count: 1,
              quarantined_count: 0,
              first_event_at: '2026-07-01T08:00:00Z',
              last_event_at: '2026-07-01T08:00:00Z',
              resolution_path: 'CURRENT_IDENTITY_EVIDENCE',
              operator_actionable: true,
            }]
          : includeActiveEnrichment
          ? [{
              source_user_key: null,
              source_kind: 'EVENT_GROUP',
              group_token: 'b'.repeat(64),
              active_user_key: user.user_key,
              uid: user.uid,
              user_id: user.user_id,
              display_name: user.display_name,
              row_version: null,
              observed_at: '2026-07-01T08:00:00Z',
              deleted_at: '2026-07-01T08:00:00Z',
              cnic_available: false,
              identity_conflict_code: null,
              event_count: 1,
              blocked_count: 1,
              quarantined_count: 0,
              first_event_at: '2026-07-01T08:00:00Z',
              last_event_at: '2026-07-01T08:00:00Z',
              resolution_path: 'ACTIVE_USER_ENRICHMENT',
              operator_actionable: true,
            }]
          : includeEventGroup
          ? [{
              source_user_key: null,
              source_kind: 'EVENT_GROUP',
              group_token: 'a'.repeat(64),
              uid: '77',
              user_id: 'CL04999',
              display_name: 'Former Employee',
              row_version: null,
              observed_at: '2026-06-01T08:00:00Z',
              deleted_at: '2026-07-01T08:00:00Z',
              cnic_available: false,
              identity_conflict_code: null,
              event_count: 2,
              blocked_count: 2,
              quarantined_count: 0,
              first_event_at: '2026-06-01T08:00:00Z',
              last_event_at: '2026-07-01T08:00:00Z',
              resolution_path: 'HR_DIRECTORY_EVENT_GROUP',
              operator_actionable: true,
            }]
          : [],
      })
    }
    if (path.includes('/api/v2/devices/connector-one/user-deletion-jobs/latest')) {
      return response({ job: null })
    }
    if (path.includes('/api/v2/devices/connector-one/users')) {
      const hasConflict = users.some((row) => row.identity_conflict_code)
      return response({
        rows: users,
        next_cursor: null,
        device,
        identity_integrity: {
          source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT',
          total_users: hasConflict ? 2 : users.length,
          with_cnic: hasConflict ? 2 : users.length,
          missing_cnic: 0,
          duplicate_groups: hasConflict ? 1 : 0,
          duplicate_users: hasConflict ? 2 : 0,
          resolved_duplicate_groups: 0,
          unresolved_duplicate_groups: hasConflict ? 1 : 0,
          unresolved_duplicate_users: hasConflict ? 2 : 0,
        },
      })
    }
    return response({ rows: [] })
  })

// People: pick the first device in the picker (07-people P1), then wait for its list.
export const selectUserTerminal = async () => {
  const link = await waitFor(() => {
    const found = document.querySelector<HTMLAnchorElement>('.people-device-list a.ui-row-link')
    expect(found).toBeTruthy()
    return found
  }, { timeout: 5000 })
  fireEvent.click(link as HTMLAnchorElement)
  await screen.findByRole('tab', { name: /People/, selected: true })
  await screen.findByRole('button', { name: /^edit /i })
}
export const selectPeopleDevice = selectUserTerminal

export class CapturingEventSource {
  static latest: CapturingEventSource | null = null
  static CLOSED = 2
  readyState = 1
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  listeners = new Map<string, Array<(event?: MessageEvent) => void>>()
  constructor(_url: string, _options?: EventSourceInit) {
    CapturingEventSource.latest = this
  }
  addEventListener(name: string, listener: (event?: MessageEvent) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) || []), listener])
  }
  close() {}
  emit(name: string, data?: string) {
    this.listeners.get(name)?.forEach((listener) => listener(data === undefined ? undefined : new MessageEvent(name, { data })))
  }
}

/**
 * The usual setup: signed in at /home with a quiet live stream and the stub
 * backend. `warm` loads a page's code once before the file's tests, so the
 * first test does not spend its time budget downloading it.
 */
export function useDashboardStubs(warm?: () => Promise<unknown>) {
  if (warm) beforeAll(async () => { await warm() }, 60_000)
  beforeEach(() => {
    window.history.replaceState({}, '', '/home')
    vi.stubGlobal('EventSource', EventSourceStub)
    vi.stubGlobal('fetch', fetchStub())
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    queryClient.clear()
  })
}
