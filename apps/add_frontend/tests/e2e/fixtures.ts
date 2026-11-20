// Shared data, the stub backend and guard fixtures for every dashboard e2e spec
// (DLV-2). `test` from here also fails a test on any Content Security Policy
// violation: `vite preview` serves the production policy (FND-1).
import { expect, test as base, type Page } from '@playwright/test'

export const test = base.extend<{ cspViolations: string[] }>({
  cspViolations: [async ({ page }, use) => {
    const violations: string[] = []
    page.on('console', (message) => {
      if (message.type() === 'error' && /Content Security Policy|Refused to (load|execute|apply|connect|frame)/i.test(message.text())) {
        violations.push(message.text())
      }
    })
    await page.addInitScript(() => {
      document.addEventListener('securitypolicyviolation', (event) => {
        console.error(`Content Security Policy violation: ${event.violatedDirective} ${event.blockedURI}`)
      })
    })
    await use(violations)
    expect(violations, 'Content Security Policy violations').toEqual([])
  }, { auto: true }],
})
export { expect }

export const device = {
  connector_id: 'connector-one',
  hardware_id: 'e0:72:a1:d6:f3:28',
  zone_id: 'ZONE-SLICTOWER-3FL',
  zone_name: 'SLICTOWER',
  device_id: '1',
  display_name: 'SLICTOWER · 3rd Floor',
  division: 'health',
  friendly_name: 'SLIC Tower · 3rd Floor',
  place: 'SLIC Tower',
  unit: '3rd Floor',
  city: 'Islamabad',
  state: 'ONLINE',
  connected: true,
  firmware_version: 'zone-lite-2.2.30',
  ota_capable: true,
  ota_state: 'OTA_READY',
  onboarding_generation: 2,
  last_onboarded_at: '2026-07-30T12:00:00Z',
  last_seen_at: '2026-08-01T18:00:00Z',
  current_activity: 'LIVE_CAPTURE',
  last_error_code: null,
  active_command: null,
  active_lease: null,
  zkt: {
    id: 1,
    serial: 'ADZV211860253', expected_serial: 'ADZV211860253', ip_address: '192.168.110.142',
    model: 'MB20/ID', platform: 'ZLM60_TFT', online: true, connection_state: 'ONLINE',
    consecutive_failures: 0, consecutive_successes: 10, flap_count_15m: 0, last_transition_at: null,
    last_online_at: null, offline_since: null, stability_since: null, backoff_until: null, probe_latency_ms: 12,
    certification_state: 'CERTIFIED', certification_observations: 2,
    capabilities: { user_write: true, create_user: true, delete_user: true, admin_lease: true }, snapshot_complete: true,
    writes_disabled_reason: null, user_count: 1, attendance_count: 42, device_time: '2026-08-01T18:00:00Z',
    device_time_sampled_at: '2026-08-01T18:00:00Z', drift_seconds: 0, last_reconcile_at: '2026-08-01T17:50:00Z', next_restart_at: null,
  },
}

/** Today's attendance totals (GET /api/v1/attendance/summary, Home). */
export const attendanceSummary = {
  from_time: '2026-08-01T19:00:00Z', to_time: null, division: null,
  totals: { received: 1038, sent: 980, waiting: 23, trying_again: 2, not_matched: 35, set_aside: 0, problem: 0, oldest_waiting_at: null },
  by_division: {
    life: { received: 312, sent: 298, waiting: 6, trying_again: 0, not_matched: 8, set_aside: 0, problem: 0, oldest_waiting_at: null },
    health: { received: 726, sent: 682, waiting: 17, trying_again: 2, not_matched: 27, set_aside: 0, problem: 0, oldest_waiting_at: null },
  },
  last_sent_at: '2026-08-01T17:56:00Z',
}

export const representativeUser = {
  id: 1, user_key: 'user-one', uid: '7', user_id: '1007', display_name: 'Ayesha Khan',
  cnic_masked: '*****-****567-1', cnic_available: true, identity_complete: true,
  identity_conflict_code: null, identity_conflict_members: [], identity_conflict_resolved: false,
  identity_resolution_id: null, shift_worker: false, privilege: 0, present: true, lifecycle_state: 'ACTIVE',
  row_version: 3, observed_at: '2026-08-12T08:00:00Z', machine_name_preview: 'Ayesha-*****-****567-1',
  current_command_state: null, read_only: false,
}

export const representativeAttendance = {
  id: 1, event_uid: 'event-one', device_serial: device.zkt.serial, uid: '7', user_id: '1007',
  display_name: 'Ayesha Khan', cnic_masked: '*****-****567-1', device_event_time: '2026-08-12T08:00:00Z',
  captured_at: '2026-08-12T08:00:01Z', received_at: '2026-08-12T08:00:02Z', source: 'LIVE',
  status: 'CAPTURED', punch: '0', clock_quality: 'OK', clock_drift_seconds: 2, ords_status: 'ACKNOWLEDGED',
  oracle_confirmed_at: '2026-08-12T08:00:05Z', oracle_confirmation_path: 'ADD_EVENT_UID',
  identity_resolution_id: null, delivery_group: 'sent',
}

/** One check-in's journey (GET /api/v1/attendance/{id}/journey, the check-in sheet). */
export const attendanceJourney = {
  id: 1, delivery_group: 'sent', ords_status: 'ACKNOWLEDGED', source: 'LIVE', live: true,
  device_event_time: '2026-08-12T08:00:00Z', captured_at: '2026-08-12T08:00:01Z', received_at: '2026-08-12T08:00:02Z',
  oracle_confirmed_at: '2026-08-12T08:00:05Z', outbox: null,
  steps: [
    { step: 'checked_in', at: '2026-08-12T08:00:00Z', done: true },
    { step: 'reached_dashboard', at: '2026-08-12T08:00:02Z', done: true },
    { step: 'queued_for_hr', at: '2026-08-12T08:00:03Z', done: true },
    { step: 'sent_to_hr', at: '2026-08-12T08:00:05Z', done: true },
  ],
}

export const nationwideDevices = [
  { ...device, connector_id: 'karachi-one', zone_id: 'ZONE-KARACHI-01', zone_name: 'Karachi', display_name: 'Karachi · Main Office', friendly_name: 'Karachi', city: 'Karachi', zkt: { ...device.zkt, id: 11, serial: 'KHI-0001' } },
  { ...device, connector_id: 'peshawar-one', zone_id: 'ZONE-PESHAWAR-01', zone_name: 'Peshawar', display_name: 'Peshawar · Ground Floor', friendly_name: 'Peshawar · Device 1', city: 'Peshawar', zkt: { ...device.zkt, id: 21, serial: 'PEW-0001' } },
  { ...device, connector_id: 'peshawar-two', zone_id: 'ZONE-PESH-02', zone_name: 'Peshawar', display_name: 'Peshawar · First Floor', friendly_name: 'Peshawar · Device 2', city: 'Peshawar', state: 'DEGRADED', zkt: { ...device.zkt, id: 22, serial: 'PEW-0002' } },
  { ...device, connector_id: 'swat-one', zone_id: 'ZONE-SWAT-01', zone_name: 'Swat', display_name: 'Swat · Regional Office', friendly_name: 'Swat', city: 'Swat', zkt: { ...device.zkt, id: 31, serial: 'SWT-0001' } },
  { ...device, connector_id: 'multan-one', zone_id: 'ZONE-MULTAN-01', zone_name: 'Multan', display_name: 'ZONE-MULTAN-01', friendly_name: 'Multan · Device 1', city: 'Multan', zkt: { ...device.zkt, id: 35, serial: 'AF4C211861133' } },
  { ...device, connector_id: 'multan-two', zone_id: 'ZONE-MULTAN-02', zone_name: 'Multan', display_name: 'ZONE-MULTAN-02', friendly_name: 'Multan · Device 2', city: 'Multan', state: 'OFFLINE', connected: false, zkt: { ...device.zkt, id: 36, serial: 'RKQ4245100152' } },
  { ...device, connector_id: 'faisalabad-one', zone_id: 'ZONE-FAISALABAD-01', zone_name: 'Faisalabad', display_name: 'ZONE-FAISALABAD-01', friendly_name: 'Faisalabad · Device 1', city: 'Faisalabad', zkt: { ...device.zkt, id: 37, serial: 'FSD-0001' } },
  { ...device, connector_id: 'faisalabad-two', zone_id: 'ZONE-FAISALABAD-02', zone_name: 'Faisalabad', display_name: 'ZONE-FAISALABAD-02', friendly_name: 'Faisalabad · Device 2', city: 'Faisalabad', zkt: { ...device.zkt, id: 38, serial: 'FSD-0002' } },
  { ...device, connector_id: 'quetta-one', zone_id: 'ZONE-QUETTA-01', zone_name: 'Quetta', display_name: 'ZONE-QUETTA-01', friendly_name: 'Quetta', city: 'Quetta', zkt: { ...device.zkt, id: 39, serial: 'UET-0001' } },
  { ...device, connector_id: 'tower-three', zone_id: 'ZONE-SLICTOWER-3FL', zone_name: 'SLICTOWER', display_name: 'SLICTOWER · 3rd Floor', zkt: { ...device.zkt, id: 41, serial: 'ISB-0003' } },
  { ...device, connector_id: 'tower-thirteen', zone_id: 'ZONE-SLICTOWER-13FL', zone_name: 'SLIC-TOWER', display_name: 'SLICTOWER · 13th Floor', friendly_name: 'SLIC Tower · 13th Floor', unit: '13th Floor', zkt: { ...device.zkt, id: 42, serial: 'ISB-0013' } },
  { ...device, connector_id: 'bld9-three', zone_id: 'LF-ZONE-BLD9-03', zone_name: 'LF-ZONE-BLD9-03', display_name: 'LF-ZONE-BLD9-03', division: 'life', friendly_name: 'Building 9 · Device 3', place: 'Building 9', unit: 'Device 3', city: 'Karachi', state: 'DEGRADED', zkt: { ...device.zkt, id: 51, serial: 'KHI-0903' } },
  { ...device, connector_id: 'bld9-one', zone_id: 'LF-ZONE-BLD9-01', zone_name: 'LF-ZONE-BLD9-01', display_name: 'LF-ZONE-BLD9-01', division: 'life', friendly_name: 'Building 9 · Device 1', place: 'Building 9', unit: 'Device 1', city: 'Karachi', zkt: { ...device.zkt, id: 52, serial: 'KHI-0901' } },
]

export const sourceException = {
  id: 1,
  connector_id: device.connector_id,
  device_id: device.device_id,
  display_name: device.display_name,
  zone_id: device.zone_id,
  terminal_serial: device.zkt.serial,
  terminal_generation: 2,
  ordinal: 5043,
  source_kind: 'TAIL',
  record_size: 40,
  disposition: 'INVALID_TIME',
  error_code: 'IMPLAUSIBLE_TERMINAL_TIME',
  raw_timestamp: 4294967295,
  observed_uid: '7',
  observed_user_id: '1007',
  raw_record_digest: 'd'.repeat(64),
  evidence_available: true,
  terminal_record_key: 'e'.repeat(64),
  attendance_event_id: null,
  observed_at: '2026-08-06T10:00:00Z',
  review_state: 'OPEN',
  reviewed_at: null,
  reviewed_by: null,
  review_reason: null,
  source_committed_cursor: 5082,
  cursor_advanced: true,
  oracle_action: 'EXCLUDED_FAIL_CLOSED',
  reviews: [],
}

export const reconciliationEvents = Array.from({ length: 899 }, (_, index) => ({
  state: index % 9 === 0 ? 'SOURCE_CAPTURE_CERTIFIED_WITH_IMMUTABLE_CHAIN_EVIDENCE' : 'CHUNK_COMMITTED',
  details: {
    start_ordinal: 89_200 - index,
    end_ordinal: 89_246 - index,
    chain_digest: `${index.toString(16).padStart(4, '0')}${'f'.repeat(60)}`,
    source_chain_digest: '9e4efdd76c75638aa3e82b0aa774ebe8093f41784091feb8840d3602037b0fae',
  },
  created_at: new Date(Date.UTC(2026, 7, 13, 7, 45, 53) - index * 1_000).toISOString(),
}))

export const reconciliationJob = {
  job_id: '11111111-1111-4111-8111-111111111111', mode: 'FULL_HISTORY_BASELINE', status: 'RUNNING', phase: 'VERIFYING_SOURCE_CHANGE', wait_reason: 'SOURCE_DIVERGENCE_PROBE_PENDING', error_code: null, error_message: null,
  operator_state: 'VERIFYING_SOURCE_CHANGE', operator_message: 'ADD is confirming a terminal-history change with independent fresh reads.', completion_outcome: null, review_required: false,
  connector: { connector_id: device.connector_id, device_id: device.device_id, display_name: device.display_name, zone_id: device.zone_id, connected: true },
  terminal: { serial: device.zkt.serial, generation: 2, cutoff_count: 5131, latest_count: 5131, record_size: 40, source_total_bytes: 205244 },
  progress: { scanned: 5100, remaining: 31, add_durable: 5100, already_present: 5098, terminal_duplicates: 0, blocked_identity: 172, quarantined: 1, oracle_target: 0, oracle_confirmed: 0, oracle_pending: 0, retry_count: 1, auto_retry_count: 1 },
  assignment: { assignment_id: null, credit_start_ordinal: null, credit_end_ordinal: null, credit_committed_through: 5100, granted_at: null, expires_at: null, accepted_at: null, heartbeat_at: null },
  eta: { low_seconds: null, high_seconds: null, confidence: 'UNAVAILABLE', unavailable_reason: 'SOURCE_DIVERGENCE_PROBE_PENDING' },
  recovery: { operation_id: '22222222-2222-4222-8222-222222222222', source_epoch: 1, source_epoch_id: '33333333-3333-4333-8333-333333333333', divergence: { divergence_id: '44444444-4444-4444-8444-444444444444', ordinal: 5130, state: 'PROBING', old_raw_digest: 'a'.repeat(64), new_raw_digest: 'b'.repeat(64), observation_count: 2, next_probe_at: '2026-08-07T06:00:00Z' } },
  events: reconciliationEvents,
  requested_at: '2026-08-07T05:01:32Z', started_at: '2026-08-07T05:01:36Z', capture_certified_at: null, oracle_certified_at: null, completed_at: null, updated_at: '2026-08-07T05:35:05Z',
}

export const attendanceRepairCandidates = {
  connector_id: device.connector_id,
  device_id: device.device_id,
  source_current: true,
  source_certificate: { certificate_digest: '5'.repeat(64) },
  date_scope: { timezone: 'Asia/Karachi', start_utc: null, end_utc_exclusive: null },
  targets: [{
    user_key: representativeUser.user_key,
    row_version: representativeUser.row_version,
    display_name: representativeUser.display_name,
    cnic_masked: representativeUser.cnic_masked,
    eligible: true,
    exclusion_code: null,
    cohorts: [{
      cohort_token: '6'.repeat(64),
      evidence_classification: 'CURRENT_USER_LINEAGE',
      selectable: true,
      exclusion_code: null,
      source_device_user_key: representativeUser.user_key,
      source_uid: '*',
      source_user_id: '****',
      first_event_at: '2026-05-01T03:15:00Z',
      last_event_at: '2026-08-20T12:15:00Z',
      event_count: 42,
      membership_digest: '7'.repeat(64),
      masked_identity: { variants: [{ display_name_masked: 'A***** K***', cnic_masked: '*****-****567-1' }], variant_count: 1, truncated: false },
      source_evidence: { terminal_manifest_events: 42, exact_tombstone: false, source_types: ['FULL_HISTORY'] },
    }, {
      cohort_token: '8'.repeat(64),
      evidence_classification: 'EXACT_TOMBSTONE',
      selectable: true,
      exclusion_code: null,
      source_device_user_key: representativeUser.user_key,
      source_uid: '**',
      source_user_id: '****',
      first_event_at: '2026-03-12T03:15:00Z',
      last_event_at: '2026-03-18T12:15:00Z',
      event_count: 6,
      membership_digest: '9'.repeat(64),
      masked_identity: { variants: [{ display_name_masked: 'O** N***', cnic_masked: '*****-****999-1' }], variant_count: 1, truncated: false },
      source_evidence: { terminal_manifest_events: 6, exact_tombstone: true, source_types: ['TOMBSTONE'] },
    }],
  }],
}

export const attendanceRepairJob = {
  job_id: '55555555-5555-4555-8555-555555555555',
  connector_id: device.connector_id,
  device_id: device.device_id,
  actor: 'StateHealthAdmin',
  status: 'AWAITING_APPROVAL',
  phase: 'PREVIEW_FROZEN',
  date_scope: attendanceRepairCandidates.date_scope,
  request_digest: '1'.repeat(64),
  preview_digest: '2'.repeat(64),
  preview_expires_at: '2026-08-28T12:15:00Z',
  source_dependency_job_id: null,
  totals: { employees: 1, events: 42, excluded: 0, completed_employees: 0, completed_events: 0, attention_events: 0 },
  wait_reason: null,
  error_code: null,
  error_message: null,
  cancellation_requested: false,
  preparation_attempt_count: 0,
  next_attempt_at: null,
  created_at: '2026-08-28T12:00:00Z',
  approved_at: null,
  started_at: null,
  completed_at: null,
  typed_confirmation: `REPAIR 1 EMPLOYEES / 42 EVENTS ON ${device.device_id} ${'2'.repeat(12)}`,
  downstream_impact: { timezone: 'Asia/Karachi', calendar_days: 42, employee_days: 42, before_identity_day_groups: 42, desired_identity_day_groups: 42, first_date: '2026-05-01', last_date: '2026-08-20' },
  targets: [{ user_key: representativeUser.user_key, display_name: representativeUser.display_name, cnic_masked: representativeUser.cnic_masked, expected_row_version: representativeUser.row_version, desired_identity_digest: '3'.repeat(64), status: 'FROZEN', event_count: 42, completed_event_count: 0, attention_event_count: 0 }],
  items: [],
  items_next_cursor: null,
}

export const attendanceReleaseQueue = {
  preview_enabled: true,
  execution_enabled: false,
  totals: { employees: 1, events: 1, eligible: 1, locked: 0 },
  rows: [{
    connector_id: device.connector_id,
    device_id: device.device_id,
    device_name: device.display_name,
    user_key: representativeUser.user_key,
    row_version: representativeUser.row_version,
    display_name: representativeUser.display_name,
    user_id: representativeUser.user_id,
    uid: representativeUser.uid,
    cnic_masked: representativeUser.cnic_masked,
    eligible: true,
    lock_reason: null,
    lock_reasons: [],
    source_current: true,
    active_release_job_id: null,
    counts: {
      ordinary_blocked: 1,
      identity_reuse: 0,
      eligible: 1,
      locked: 0,
      in_progress: 0,
    },
    first_event_at: '2026-08-12T08:00:00Z',
    last_event_at: '2026-08-12T08:00:00Z',
  }],
  next_cursor: null,
}

export const attendanceReleaseCandidates = {
  candidate_set_token: 'candidate-set-token',
  expires_at: '2099-08-12T10:15:00Z',
  source_current: true,
  source_certificate: { certificate_digest: 'c'.repeat(64) },
  target: {
    user_key: representativeUser.user_key,
    row_version: representativeUser.row_version,
    display_name: representativeUser.display_name,
    user_id: representativeUser.user_id,
    uid: representativeUser.uid,
    cnic_masked: representativeUser.cnic_masked,
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
    all: 1,
    eligible: 1,
    locked: 0,
    ordinary_blocked: 1,
    identity_reuse: 0,
  },
  rows: [{
    event_token: 'event-token-one',
    event_uid: 'release-event-one',
    device_event_time: '2026-08-12T08:00:00Z',
    punch: '0',
    status: '0',
    source: 'FULL_HISTORY',
    device_serial: device.zkt.serial,
    uid: representativeUser.uid,
    user_id: representativeUser.user_id,
    display_name: representativeUser.display_name,
    clock_quality: 'OK',
    source_ords_status: 'BLOCKED_IDENTITY',
    risk_class: 'ORDINARY_BLOCKED',
    evidence_classification: 'CURRENT_USER_LINEAGE',
    eligible: true,
    lock_reason: null,
  }],
  next_cursor: null,
}

export const factoryBundle = {
  bundle_id: 'zone-lite-2.4.5-6132c9b773b9', hardware_profile: 'esp32s3-16mb-zone-lite-v1',
  version: '2.4.5', git_sha: '6132c9b773b9a4016173e9b99dfde6ccc5dc29e5',
  partition_layout: 'zone-lite-factory-v1', manifest_sha256: 'f'.repeat(64),
  signing_key_ids: ['active', 'reserve-1', 'reserve-2'], images: [], state: 'AVAILABLE',
  published_at: '2026-08-09T10:00:00Z',
}

export const installerApps = {
  latest: {
    version: '1.1.0', git_sha: 'f'.repeat(40), published_at: '2026-11-04T09:30:00Z',
    files: [
      { platform: 'windows', arch: 'x64', kind: 'setup', filename: 'StateLife-Device-Installer-Setup-1.1.0.exe', size: 86_123_456, sha256: 'd'.repeat(64), download_path: '/api/v1/installer/apps/1.1.0/StateLife-Device-Installer-Setup-1.1.0.exe' },
      { platform: 'macos', arch: 'arm64', kind: 'dmg', filename: 'StateLife-Device-Installer-1.1.0-macOS-arm64.dmg', size: 99_614_720, sha256: 'e'.repeat(64), download_path: '/api/v1/installer/apps/1.1.0/StateLife-Device-Installer-1.1.0-macOS-arm64.dmg' },
    ],
  },
  min_app_version: '1.0.0',
}

export async function mockDashboard(page: Page) {
  await page.route('**/events/**', (route) => route.abort())
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    let json: unknown = { rows: [] }
    if (url.pathname === '/api/v1/auth/session') json = { username: 'StateHealthAdmin', csrf_token: 'test-token' }
    else if (url.pathname === '/api/v1/overview') json = { total: 1, online: 1, offline: 0, degraded: 0, flapping: 0, open_alerts: 1, active_leases: 0, ords_delivery: { backlog: 12, retrying: 0, blocked_identity: 2, quarantined: 1 } }
    else if (url.pathname === '/api/v1/devices') json = { rows: [device] }
    else if (url.pathname === '/api/v1/attendance/summary') json = attendanceSummary
    else if (/^\/api\/v1\/attendance\/\d+\/journey$/.test(url.pathname)) json = attendanceJourney
    else if (url.pathname === `/api/v1/devices/${device.connector_id}`) json = device
    else if (url.pathname.endsWith('/zkt-custody')) json = {
      connector_id: device.connector_id, enabled: true, sampled_at: new Date().toISOString(),
      oracle_completion: 'NOT_ASSERTED', missing_processing_obligation: true,
      counts: [{ state: 'WAIT_PROFILE', owner: 'ADD_PROTOCOL', count: 12 }],
      rows: [{ id: 1, state: 'WAIT_PROFILE', reason_code: 'PROFILE_QUALIFICATION_REQUIRED',
        owner: 'ADD_PROTOCOL', updated_at: new Date().toISOString(), next_attempt_at: null,
        decoding: { version: 'synthetic-v1', result: 'UNQUALIFIED_FACTS', authority: 'UNQUALIFIED',
          current_input: true, current_decoder: true, step_index: 0, sampled_at: new Date().toISOString() } }], next_cursor: null,
      processor: { schema_version: 1, instance_id: '11111111-2222-4333-8444-555555555555',
        sampled_at: new Date().toISOString(), state: 'STALLED',
        last_completed_at: new Date(Date.now() - 6_000).toISOString(), last_progress_at: null,
        inspected_groups_total: 0, successful_ticks: 3, failed_ticks: 1, last_tick_ms: 8 },
    }
    else if (url.pathname.includes('/connectivity')) json = { rows: [] }
    else if (url.pathname.includes('/logs')) json = { rows: [], next_cursor: null }
    else if (url.pathname === '/api/v1/alerts') json = { rows: [{ id: 1, code: 'ZKT_CLOCK_DRIFT', severity: 'WARNING', state: 'OPEN', message: 'Terminal clock requires review.', details: {}, first_seen_at: '2026-08-01T12:00:00Z', last_seen_at: '2026-08-01T17:00:00Z', acknowledged_at: null, resolved_at: null, device: { connector_id: device.connector_id, display_name: device.display_name, zone_id: device.zone_id, hardware_id: device.hardware_id } }], next_cursor: null, totals: { all: 1, open: 1, acknowledged: 0, resolved: 0 } }
    else if (url.pathname.endsWith('/alerts')) json = { rows: [] }
    else if (url.pathname === '/api/v1/attendance') json = { rows: [representativeAttendance], next_cursor: null }
    else if (url.pathname === '/api/v2/attendance-force-releases' || url.pathname === '/api/v2/attendance-recovery/checks') json = { enabled: true, rows: [], next_cursor: null }
    else if (url.pathname === '/api/v2/attendance-release-queue') json = attendanceReleaseQueue
    else if (url.pathname === `/api/v2/devices/${device.connector_id}/attendance-release-candidates/query`) json = attendanceReleaseCandidates
    else if (url.pathname === '/api/v1/firmware/releases') json = { enabled: true, hil_enabled: false, rows: [{ release_id: 'release-2.2.30', version: '2.2.30', git_sha: 'a'.repeat(40), image_sha256: 'b'.repeat(64), application_sha256: 'c'.repeat(64), image_size: 1024, state: 'AVAILABLE', partition_layout: 'ota-v2', signing_key_id: 'production-key', published_at: '2026-07-30T12:00:00Z', revoked_at: null, revoked_by: null, hil_target_mac: null }], next_cursor: null, filtered_total: 1, totals: { all: 1, available: 1, hil_only: 0, revoked: 0 } }
    else if (url.pathname === '/api/v1/firmware/campaigns') json = { enabled: true, hil_enabled: false, rows: [], next_cursor: null, filtered_total: 0, totals: { campaigns: { all: 0 }, deployments: { all: 0 } } }
    else if (url.pathname === '/api/v1/provisioning/capabilities') json = { enabled: true, supported_platforms: ['windows-x64', 'macos-arm64'], hardware_profile: 'esp32s3-16mb-zone-lite-v1', companion_min_version: '1.0.0', latest_bundle: factoryBundle, can_start: true }
    else if (url.pathname === '/api/v1/provisioning/companions') json = { rows: [] }
    else if (url.pathname === '/api/v1/provisioning/sessions') json = { rows: [] }
    else if (url.pathname === '/api/v1/installer/apps') json = installerApps
    else if (url.pathname === '/api/v1/provisioning/companion-releases/latest') json = { platform: url.searchParams.get('platform'), version: '1.0.0', filename: 'add-provisioning-companion-windows-x64.exe', sha256: 'c'.repeat(64), size: 123456, git_sha: factoryBundle.git_sha, download_url: '/api/v1/provisioning/companion-releases/windows-x64/download', os_signed: false }
    else if (url.pathname === `/api/v1/devices/${device.connector_id}/attendance-repairs/preflight`) json = { preview_enabled: true, execution_enabled: true, eligible: true, ready_now: true, requires_source_reconciliation: false, hard_blockers: [], waitable_blockers: [], limits: { employees: 500, events: 250000, oracle_batch: 100 }, source_certificate: attendanceRepairCandidates.source_certificate, terminal: { serial: device.zkt.serial, snapshot_complete: true, snapshot_stable: true, snapshot_revision: 9, attendance_count: 42 }, oracle: { available: true, capabilities: { contract_version: '1' } }, worker: { active_jobs: 0, review_items: 0, stale_leases: 0, oldest_job_age_seconds: 0 } }
    else if (url.pathname === `/api/v1/devices/${device.connector_id}/attendance-repair-candidates/query`) json = attendanceRepairCandidates
    else if (url.pathname === `/api/v1/devices/${device.connector_id}/attendance-repairs/prepare`) json = attendanceRepairJob
    else if (url.pathname === `/api/v1/attendance-repairs/${attendanceRepairJob.job_id}`) json = attendanceRepairJob
    else if (url.pathname === '/api/v1/attendance-repairs') json = { preview_enabled: true, execution_enabled: true, rows: [], next_cursor: null, totals: { all: 0, active: 0, attention: 0 }, worker: { active_jobs: 0, review_items: 0, stale_leases: 0, oldest_job_age_seconds: 0 } }
    else if (url.pathname === '/api/v1/reconciliations') json = { enabled: true, scheduler: { policy: 'BOUNDED_PARALLEL_PER_DEVICE', device_concurrency: 6, active_scan_jobs: 1, waiting_scan_jobs: 0, available_scan_slots: 5, history_backlog: 0, history_backlog_limit: 10000, reserved_credit: 0, available_credit: 10000 }, rows: [{ ...reconciliationJob, events: undefined }], next_cursor: null, filtered_total: 1, totals: { all: 1, active: 1, queued_waiting: 0, paused: 0, attention: 0, completed: 0, cancelled: 0 } }
    else if (url.pathname === `/api/v1/reconciliations/${reconciliationJob.job_id}`) json = reconciliationJob
    else if (url.pathname.endsWith('/reconciliations/preflight')) json = { eligible: true, ready_now: true, hard_blockers: [], waitable_blockers: [], connector: { connector_id: device.connector_id, device_id: device.device_id, display_name: device.display_name, zone_id: device.zone_id, connected: true, firmware_version: device.firmware_version }, terminal: { serial: device.zkt.serial, attendance_count: 42, user_count: 1, connection_state: 'ONLINE', identity_snapshot_revision: 1, range_resume_verified: true }, coverage: null }
    else if (url.pathname === '/api/v1/source-exceptions/1/review') json = { ...sourceException, review_state: 'REVIEWED', reviewed_at: '2026-08-06T10:10:00Z', reviewed_by: 'StateHealthAdmin', review_reason: 'Reviewed immutable source evidence.', reviews: [{ review_id: 'review-one', state: 'REVIEWED', reason: 'Reviewed immutable source evidence.', actor: 'StateHealthAdmin', created_at: '2026-08-06T10:10:00Z' }] }
    else if (url.pathname === '/api/v1/source-exceptions/1/reveal') json = { id: 1, raw_record_b64: '//////////8=', raw_record_hex: 'ffffffffffffffff', raw_record_digest: sourceException.raw_record_digest, record_size: 40 }
    else if (url.pathname === '/api/v1/source-exceptions/1') json = sourceException
    else if (url.pathname === '/api/v1/source-exceptions') json = { totals: { all: 1, open: 1, reviewed: 0, invalid_time: 1, malformed: 0, affected_terminals: 1 }, rows: [sourceException], next_cursor: null, filtered_total: 1 }
    else if (url.pathname === '/api/v1/reconciliation-divergences/44444444-4444-4444-8444-444444444444/reveal') json = { divergence_id: '44444444-4444-4444-8444-444444444444', raw_record_b64: '//////////8=', raw_record_hex: 'ffffffffffffffff', new_raw_digest: 'b'.repeat(64) }
    else if (url.pathname === '/api/v1/reconciliation-divergences/44444444-4444-4444-8444-444444444444') json = { divergence_id: '44444444-4444-4444-8444-444444444444', job_id: reconciliationJob.job_id, ordinal: 5130, state: 'PROBING', old_raw_digest: 'a'.repeat(64), new_raw_digest: 'b'.repeat(64), old_disposition: 'INVALID_TIME', new_disposition: 'INVALID_TIME', observations: [{ raw_record_digest: 'b'.repeat(64), disposition: 'INVALID_TIME', observed_at: '2026-08-07T05:26:06Z', kind: 'RECONCILIATION_CHUNK' }, { raw_record_digest: 'b'.repeat(64), disposition: 'INVALID_TIME', observed_at: '2026-08-07T05:27:06Z', kind: 'FRESH_SOURCE_PROBE' }], evidence_available: true, created_at: '2026-08-07T05:26:06Z', resolved_at: null }
    else if (url.pathname.includes('/historical-identities')) json = { totals: { unresolved_events: 0, blocked_identity: 0, quarantined_identity_reuse: 0, unassigned_events: 0, actionable_event_groups: 0 }, rows: [], unassigned_groups: [] }
    else if (url.pathname.includes('/identity-conflicts')) json = { raw_duplicate_groups: 0, resolved_groups: 0, unresolved_groups: 0, groups: [], evidence_scope: { add_attendance_count: 0, terminal_attendance_count: 0, attendance_coverage_percent: 0 } }
    else if (url.pathname.includes('/user-deletion-jobs/latest')) json = { job: null }
    else if (url.pathname.includes('/users')) json = { rows: [representativeUser], next_cursor: null, device, identity_integrity: { source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT', total_users: 1, with_cnic: 1, missing_cnic: 0, duplicate_groups: 0, duplicate_users: 0, resolved_duplicate_groups: 0, unresolved_duplicate_groups: 0, unresolved_duplicate_users: 0 } }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(json) })
  })
}

/** The browser may keep only `add.prefs` (D-25) and nothing in session storage. */
export async function expectOnlyAllowedStorage(page: Page) {
  const stored = await page.evaluate(() => ({ local: Object.keys(localStorage), session: sessionStorage.length }))
  expect(stored.local.filter((key) => key !== 'add.prefs')).toEqual([])
  expect(stored.session).toBe(0)
}

/** Every page of the dashboard, for sweeps and the screenshot tour. */
export const DASHBOARD_ROUTES = [
  '/home', '/devices', '/devices/connector-one', '/attendance', '/problems', '/people', '/people/connector-one',
  '/recover', '/updates', '/setup', '/settings',
] as const
