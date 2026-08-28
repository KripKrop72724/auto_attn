import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

const device = {
  connector_id: 'connector-one',
  hardware_id: 'e0:72:a1:d6:f3:28',
  zone_id: 'ZONE-SLICTOWER-3FL',
  zone_name: 'SLICTOWER',
  device_id: '1',
  display_name: 'SLICTOWER · 3rd Floor',
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

const representativeUser = {
  id: 1, user_key: 'user-one', uid: '7', user_id: '1007', display_name: 'Ayesha Khan',
  cnic_masked: '*****-****567-1', cnic_available: true, identity_complete: true,
  identity_conflict_code: null, identity_conflict_members: [], identity_conflict_resolved: false,
  identity_resolution_id: null, shift_worker: false, privilege: 0, present: true, lifecycle_state: 'ACTIVE',
  row_version: 3, observed_at: '2026-08-12T08:00:00Z', machine_name_preview: 'Ayesha-*****-****567-1',
  current_command_state: null, read_only: false,
}

const representativeAttendance = {
  id: 1, event_uid: 'event-one', device_serial: device.zkt.serial, uid: '7', user_id: '1007',
  display_name: 'Ayesha Khan', cnic_masked: '*****-****567-1', device_event_time: '2026-08-12T08:00:00Z',
  captured_at: '2026-08-12T08:00:01Z', received_at: '2026-08-12T08:00:02Z', source: 'LIVE',
  status: 'CAPTURED', punch: '0', clock_quality: 'OK', clock_drift_seconds: 2, ords_status: 'ACKNOWLEDGED',
  oracle_confirmed_at: '2026-08-12T08:00:05Z', oracle_confirmation_path: 'ADD_EVENT_UID',
  identity_resolution_id: null,
}

const nationwideDevices = [
  { ...device, connector_id: 'karachi-one', zone_id: 'ZONE-KARACHI-01', zone_name: 'Karachi', display_name: 'Karachi · Main Office', zkt: { ...device.zkt, id: 11, serial: 'KHI-0001' } },
  { ...device, connector_id: 'peshawar-one', zone_id: 'ZONE-PESHAWAR-01', zone_name: 'Peshawar', display_name: 'Peshawar · Ground Floor', zkt: { ...device.zkt, id: 21, serial: 'PEW-0001' } },
  { ...device, connector_id: 'peshawar-two', zone_id: 'ZONE-PESH-02', zone_name: 'Peshawar', display_name: 'Peshawar · First Floor', state: 'DEGRADED', zkt: { ...device.zkt, id: 22, serial: 'PEW-0002' } },
  { ...device, connector_id: 'swat-one', zone_id: 'ZONE-SWAT-01', zone_name: 'Swat', display_name: 'Swat · Regional Office', zkt: { ...device.zkt, id: 31, serial: 'SWT-0001' } },
  { ...device, connector_id: 'multan-one', zone_id: 'ZONE-MULTAN-01', zone_name: 'Multan', display_name: 'ZONE-MULTAN-01', zkt: { ...device.zkt, id: 35, serial: 'AF4C211861133' } },
  { ...device, connector_id: 'multan-two', zone_id: 'ZONE-MULTAN-02', zone_name: 'Multan', display_name: 'ZONE-MULTAN-02', zkt: { ...device.zkt, id: 36, serial: 'RKQ4245100152' } },
  { ...device, connector_id: 'faisalabad-one', zone_id: 'ZONE-FAISALABAD-01', zone_name: 'Faisalabad', display_name: 'ZONE-FAISALABAD-01', zkt: { ...device.zkt, id: 37, serial: 'FSD-0001' } },
  { ...device, connector_id: 'faisalabad-two', zone_id: 'ZONE-FAISALABAD-02', zone_name: 'Faisalabad', display_name: 'ZONE-FAISALABAD-02', zkt: { ...device.zkt, id: 38, serial: 'FSD-0002' } },
  { ...device, connector_id: 'quetta-one', zone_id: 'ZONE-QUETTA-01', zone_name: 'Quetta', display_name: 'ZONE-QUETTA-01', zkt: { ...device.zkt, id: 39, serial: 'UET-0001' } },
  { ...device, connector_id: 'tower-three', zone_id: 'ZONE-SLICTOWER-3FL', zone_name: 'SLICTOWER', display_name: 'SLICTOWER · 3rd Floor', zkt: { ...device.zkt, id: 41, serial: 'ISB-0003' } },
  { ...device, connector_id: 'tower-thirteen', zone_id: 'ZONE-SLICTOWER-13FL', zone_name: 'SLIC-TOWER', display_name: 'SLICTOWER · 13th Floor', zkt: { ...device.zkt, id: 42, serial: 'ISB-0013' } },
]

const sourceException = {
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

const reconciliationEvents = Array.from({ length: 899 }, (_, index) => ({
  state: index % 9 === 0 ? 'SOURCE_CAPTURE_CERTIFIED_WITH_IMMUTABLE_CHAIN_EVIDENCE' : 'CHUNK_COMMITTED',
  details: {
    start_ordinal: 89_200 - index,
    end_ordinal: 89_246 - index,
    chain_digest: `${index.toString(16).padStart(4, '0')}${'f'.repeat(60)}`,
    source_chain_digest: '9e4efdd76c75638aa3e82b0aa774ebe8093f41784091feb8840d3602037b0fae',
  },
  created_at: new Date(Date.UTC(2026, 7, 13, 7, 45, 53) - index * 1_000).toISOString(),
}))

const reconciliationJob = {
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

const attendanceRepairCandidates = {
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

const attendanceRepairJob = {
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

const factoryBundle = {
  bundle_id: 'zone-lite-2.4.5-6132c9b773b9', hardware_profile: 'esp32s3-16mb-zone-lite-v1',
  version: '2.4.5', git_sha: '6132c9b773b9a4016173e9b99dfde6ccc5dc29e5',
  partition_layout: 'zone-lite-factory-v1', manifest_sha256: 'f'.repeat(64),
  signing_key_ids: ['active', 'reserve-1', 'reserve-2'], images: [], state: 'AVAILABLE',
  published_at: '2026-08-09T10:00:00Z',
}

async function mockDashboard(page: Page) {
  await page.route('**/events/**', (route) => route.abort())
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    let json: unknown = { rows: [] }
    if (url.pathname === '/api/v1/auth/session') json = { username: 'StateHealthAdmin', csrf_token: 'test-token' }
    else if (url.pathname === '/api/v1/overview') json = { total: 1, online: 1, offline: 0, degraded: 0, flapping: 0, open_alerts: 1, active_leases: 0, ords_delivery: { backlog: 12, retrying: 0, blocked_identity: 2, quarantined: 1 } }
    else if (url.pathname === '/api/v1/devices') json = { rows: [device] }
    else if (url.pathname === `/api/v1/devices/${device.connector_id}`) json = device
    else if (url.pathname.includes('/connectivity')) json = { rows: [] }
    else if (url.pathname.includes('/logs')) json = { rows: [], next_cursor: null }
    else if (url.pathname === '/api/v1/alerts') json = { rows: [{ id: 1, code: 'ZKT_CLOCK_DRIFT', severity: 'WARNING', state: 'OPEN', message: 'Terminal clock requires review.', details: {}, first_seen_at: '2026-08-01T12:00:00Z', last_seen_at: '2026-08-01T17:00:00Z', acknowledged_at: null, resolved_at: null, device: { connector_id: device.connector_id, display_name: device.display_name, zone_id: device.zone_id, hardware_id: device.hardware_id } }], next_cursor: null, totals: { all: 1, open: 1, acknowledged: 0, resolved: 0 } }
    else if (url.pathname.endsWith('/alerts')) json = { rows: [] }
    else if (url.pathname === '/api/v1/attendance') json = { rows: [representativeAttendance], next_cursor: null }
    else if (url.pathname === '/api/v1/firmware/releases') json = { enabled: true, hil_enabled: false, rows: [{ release_id: 'release-2.2.30', version: '2.2.30', git_sha: 'a'.repeat(40), image_sha256: 'b'.repeat(64), application_sha256: 'c'.repeat(64), image_size: 1024, state: 'AVAILABLE', partition_layout: 'ota-v2', signing_key_id: 'production-key', published_at: '2026-07-30T12:00:00Z', revoked_at: null, revoked_by: null, hil_target_mac: null }], next_cursor: null, filtered_total: 1, totals: { all: 1, available: 1, hil_only: 0, revoked: 0 } }
    else if (url.pathname === '/api/v1/firmware/campaigns') json = { enabled: true, hil_enabled: false, rows: [], next_cursor: null, filtered_total: 0, totals: { campaigns: { all: 0 }, deployments: { all: 0 } } }
    else if (url.pathname === '/api/v1/provisioning/capabilities') json = { enabled: true, supported_platforms: ['windows-x64', 'macos-arm64'], hardware_profile: 'esp32s3-16mb-zone-lite-v1', companion_min_version: '1.0.0', latest_bundle: factoryBundle, can_start: true }
    else if (url.pathname === '/api/v1/provisioning/companions') json = { rows: [] }
    else if (url.pathname === '/api/v1/provisioning/sessions') json = { rows: [] }
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

test.beforeEach(async ({ page }) => mockDashboard(page))

test('adaptive shell has no horizontal overflow and meets critical accessibility checks', async ({ page }) => {
  await page.goto('/fleet')
  await expect(page.getByRole('heading', { name: 'Attendance device command center' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Pakistan device network map' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Map' })).toHaveAttribute('aria-pressed', 'true')
  const islamabadMarker = page.getByRole('button', { name: /Islamabad, 1 device, All online/i })
  await islamabadMarker.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: 'Inspect SLICTOWER · 3rd Floor' })).toBeVisible()
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect(page.locator('.fleet-map-marker-ripple')).toHaveCSS('display', 'none')
  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    content: document.documentElement.scrollWidth,
    offenders: Array.from(document.querySelectorAll<HTMLElement>('body *'))
      .filter((element) => element.getBoundingClientRect().right > window.innerWidth + 1)
      .slice(0, 8)
      .map((element) => ({ tag: element.tagName, className: element.className, parent: element.parentElement?.className, right: Math.round(element.getBoundingClientRect().right), scrollWidth: element.scrollWidth })),
  }))
  expect(dimensions.content, JSON.stringify(dimensions.offenders)).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
})

test('nationwide fleet keeps clustered city beacons stable and location details contextual', async ({ page }, testInfo) => {
  await page.route('**/api/v1/devices*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ rows: nationwideDevices }),
  }))
  await page.route('**/api/v1/overview*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ total: 11, online: 10, offline: 0, degraded: 1, flapping: 0, quarantined_duplicate_serial: 0, open_alerts: 1, active_leases: 0, ords_delivery: { backlog: 0, retrying: 0, blocked_identity: 0, quarantined: 0 } }),
  }))

  await page.goto('/fleet')
  await expect(page.getByRole('heading', { name: 'Attendance device command center' })).toBeVisible()
  const markers = page.locator('.fleet-map-marker')
  await expect(markers).toHaveCount(7, { timeout: 10_000 })
  const projectedCoordinates = await markers.evaluateAll((nodes) => Object.fromEntries(nodes.map((node) => {
    const location = Array.from(node.classList).find((name) => name.startsWith('location-'))?.replace('location-', '') || ''
    const style = (node as HTMLElement).style
    return [location, {
      x: Number.parseFloat(style.getPropertyValue('--marker-x')),
      y: Number.parseFloat(style.getPropertyValue('--marker-y')),
    }]
  })))
  const expectedCoordinates = {
    swat: { x: 69.21, y: 20.98 },
    peshawar: { x: 64.50, y: 25.91 },
    islamabad: { x: 73.09, y: 28.07 },
    faisalabad: { x: 73.58, y: 42.65 },
    quetta: { x: 38.85, y: 50.95 },
    multan: { x: 64.50, y: 51.09 },
    karachi: { x: 39.00, y: 85.67 },
  }
  for (const [location, expected] of Object.entries(expectedCoordinates)) {
    expect(projectedCoordinates[location].x).toBeCloseTo(expected.x, 1)
    expect(projectedCoordinates[location].y).toBeCloseTo(expected.y, 1)
  }

  const markerCores = await page.locator('.fleet-map-marker-core').evaluateAll((nodes) => nodes.map((node) => {
    const box = node.getBoundingClientRect()
    return { x: box.x + box.width / 2, y: box.y + box.height / 2, radius: box.width / 2 }
  }))
  const closestVisibleGap = Math.min(...markerCores.flatMap((left, index) => markerCores.slice(index + 1).map((right) => (
    Math.hypot(left.x - right.x, left.y - right.y) - left.radius - right.radius
  ))))
  expect(closestVisibleGap).toBeGreaterThanOrEqual(-1)

  const peshawar = page.getByRole('button', { name: /Peshawar, 2 devices, Needs attention/i })
  await page.locator('.page-content > section.panel').evaluate(async (node) => {
    await Promise.all(node.getAnimations().map((animation) => animation.finished.catch(() => undefined)))
  })
  const beforeSelection = await peshawar.evaluate((node) => {
    const box = node.getBoundingClientRect()
    const stage = node.closest('.fleet-map-stage')?.getBoundingClientRect()
    return { x: box.x - (stage?.x || 0), y: box.y - (stage?.y || 0) }
  })
  await peshawar.click()
  await expect(page.getByRole('heading', { name: 'Peshawar' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Inspect Peshawar/ })).toHaveCount(2)
  const afterSelection = await peshawar.evaluate((node) => {
    const box = node.getBoundingClientRect()
    const stage = node.closest('.fleet-map-stage')?.getBoundingClientRect()
    return { x: box.x - (stage?.x || 0), y: box.y - (stage?.y || 0) }
  })
  expect(Math.abs(afterSelection.x - beforeSelection.x)).toBeLessThan(1)
  expect(Math.abs(afterSelection.y - beforeSelection.y)).toBeLessThan(1)

  await page.getByRole('button', { name: /Islamabad, 2 devices, All online/i }).click()
  await expect(page.getByRole('heading', { name: 'Islamabad' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Inspect SLICTOWER/ })).toHaveCount(2)
  await page.getByRole('button', { name: /Multan, 2 devices, All online/i }).click()
  await expect(page.getByRole('heading', { name: 'Multan' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Inspect ZONE-MULTAN/ })).toHaveCount(2)
  await page.getByRole('button', { name: /Faisalabad, 2 devices, All online/i }).click()
  await expect(page.getByRole('heading', { name: 'Faisalabad' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Inspect ZONE-FAISALABAD/ })).toHaveCount(2)
  await page.getByRole('button', { name: /Quetta, 1 device, All online/i }).click()
  await expect(page.getByRole('heading', { name: 'Quetta' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Inspect ZONE-QUETTA-01' })).toBeVisible()
  if ((page.viewportSize()?.width || 0) > 920) {
    const canvasBox = await page.locator('.fleet-map-canvas').boundingBox()
    const sheetBox = await page.locator('.fleet-location-sheet').boundingBox()
    expect((sheetBox?.width || 0)).toBeLessThanOrEqual(400)
    expect((sheetBox?.height || 0)).toBeLessThan(canvasBox?.height || 0)
  }
  if (process.env.ADD_VISUAL_QA === '1') await page.screenshot({ path: testInfo.outputPath('nationwide-map.png'), fullPage: true })
})

test('primary routes and device deep link remain usable', async ({ page }) => {
  await page.goto('/fleet')
  await page.getByRole('button', { name: /Islamabad, 1 device, All online/i }).click()
  await page.getByRole('button', { name: 'Inspect SLICTOWER · 3rd Floor' }).click()
  await expect(page).toHaveURL(/\/fleet\/connector-one$/)
  await expect(page.getByRole('dialog', { name: 'SLICTOWER · 3rd Floor' })).toBeVisible()
  await page.getByRole('button', { name: 'Close dialog' }).click()

  const primaryNav = page.getByRole('navigation', { name: 'Primary navigation' })
  await primaryNav.getByRole('button', { name: 'Users', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Device users' })).toBeVisible()
  await primaryNav.getByRole('button', { name: 'Attendance', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Live attendance' })).toBeVisible()
  if ((page.viewportSize()?.width || 0) <= 760) {
    const moreTrigger = page.getByRole('button', { name: 'More', exact: true })
    await moreTrigger.click()
    await expect(page.getByRole('dialog', { name: 'More operations' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog', { name: 'More operations' })).toHaveCount(0)
    await expect(moreTrigger).toBeFocused()
    await moreTrigger.click()
    const moreNavigation = page.getByRole('dialog', { name: 'More operations' })
    await moreNavigation.getByRole('button', { name: /Reconciliation/ }).click()
    await expect(page.getByRole('heading', { name: 'Terminal truth & recovery' })).toBeVisible()
    await page.getByRole('button', { name: 'More', exact: true }).click()
    await page.getByRole('dialog', { name: 'More operations' }).getByRole('button', { name: /Firmware/ }).click()
  } else {
    await primaryNav.getByRole('button', { name: 'Reconciliation', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Terminal truth & recovery' })).toBeVisible()
    await primaryNav.getByRole('button', { name: 'Firmware', exact: true }).click()
  }
  await expect(page.getByRole('heading', { name: 'Firmware operations' })).toBeVisible()
  const channelBadgeGeometry = await page.locator('.firmware-channel-strip .status-badge').first().evaluate((badge) => {
    const icon = badge.querySelector('svg')?.getBoundingClientRect()
    const text = badge.querySelector('span')?.getBoundingClientRect()
    const box = badge.getBoundingClientRect()
    return {
      display: getComputedStyle(badge).display,
      flexDirection: getComputedStyle(badge).flexDirection,
      height: box.height,
      horizontal: Boolean(icon && text && icon.right <= text.left && Math.abs((icon.top + icon.height / 2) - (text.top + text.height / 2)) <= 2),
    }
  })
  expect(['flex', 'inline-flex']).toContain(channelBadgeGeometry.display)
  expect(channelBadgeGeometry.flexDirection).toBe('row')
  expect(channelBadgeGeometry.height).toBeLessThanOrEqual(32)
  expect(channelBadgeGeometry.horizontal).toBe(true)
  await page.getByRole('tab', { name: /Signed releases/ }).click()
  await expect(page.getByRole('heading', { name: 'Signed release inventory' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Zone Lite 2.2.30' })).toBeVisible()
  await page.getByRole('tab', { name: /Campaigns/ }).click()
  await expect(page.getByRole('heading', { name: 'Campaign operations' })).toBeVisible()
  await primaryNav.getByRole('button', { name: /Alerts/ }).click()
  await expect(page.getByRole('heading', { name: 'Alerts and exceptions' })).toBeVisible()
  expect(page.url()).not.toMatch(/cnic|password|reason|confirmation/i)
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 })
})

test('populated Users and Attendance workspaces are responsive, keyboard-operable, and accessible', async ({ page }) => {
  await page.goto('/users/connector-one')
  await expect(page.getByRole('heading', { name: 'Device users' })).toBeVisible()
  await expect(page.getByRole('article', { name: /Ayesha Khan, user 1007/i })).toBeVisible()
  await expect(page.getByRole('tab', { name: /Directory/i })).toHaveAttribute('aria-selected', 'true')
  const more = page.getByLabel('More actions for Ayesha Khan')
  await more.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('group', { name: 'Actions for Ayesha Khan' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(more).toBeFocused()
  let dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  let results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])

  await page.goto('/attendance')
  await expect(page.getByRole('heading', { name: 'Live attendance' })).toBeVisible()
  const attendance = page.getByRole('article', { name: /Ayesha Khan, Check in/i })
  await expect(attendance).toBeVisible()
  const details = attendance.getByLabel(/View event details/i)
  await details.focus()
  await page.keyboard.press('Enter')
  const eventUid = attendance.getByText('event-one')
  await expect(eventUid).toBeVisible()
  await eventUid.scrollIntoViewIfNeeded()
  dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  if ((page.viewportSize()?.width || 0) <= 760) {
    const eventBox = await eventUid.boundingBox()
    const navigationBox = await page.locator('.app-sidebar').boundingBox()
    expect((eventBox?.y || 0) + (eventBox?.height || 0)).toBeLessThanOrEqual((navigationBox?.y || page.viewportSize()?.height || 0) + 1)
  }
  results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
  expect(page.url()).not.toMatch(/cnic|password|reason|confirmation/i)
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 })
})

test('employee repair gives a simple, safe, and responsive guided flow', async ({ page }, testInfo) => {
  await page.goto('/reconciliation?tab=employee-repair&device_id=connector-one')
  await expect(page.getByRole('heading', { name: 'Fix past attendance for an employee' })).toBeVisible()
  await expect(page.getByText('People can keep punching')).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Repair steps' })).toContainText('Machine')
  await expect(page.getByRole('navigation', { name: 'Repair steps' })).toContainText('Done')

  const employee = page.getByRole('checkbox', { name: 'Select Ayesha Khan' })
  await employee.focus()
  await page.keyboard.press('Space')
  await expect(employee).toBeChecked()
  await page.getByRole('button', { name: 'Review past punches' }).click()

  await expect(page.getByRole('heading', { name: 'Review the past punches we found' })).toBeVisible()
  await expect(page.getByText('42 punches').first()).toBeVisible()
  await expect(page.getByText('Only the employee name and CNIC can be fixed')).toBeVisible()
  const olderRecords = page.getByText(/Possible older employee records \(1\)/)
  await olderRecords.click()
  await expect(page.getByText(/Previous details O\*\* N\*\*\*/)).toBeVisible()
  await expect(page.getByText('Not safe to include automatically')).toHaveCount(0)

  let dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  if (process.env.ADD_VISUAL_QA === '1') await page.screenshot({ path: testInfo.outputPath('employee-repair-review.png'), fullPage: true })

  await page.getByRole('button', { name: 'Prepare final check' }).click()
  await expect(page.getByRole('heading', { name: 'Waiting for approval' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Ready for your final confirmation' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Check once more, then start the repair' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Start the repair' })).toBeDisabled()

  dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
  if (process.env.ADD_VISUAL_QA === '1') await page.screenshot({ path: testInfo.outputPath('employee-repair-confirm.png'), fullPage: true })
})

test('terminal multi-selection survives server-side search and current-view toggles', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-1280', 'Selection-state regression runs once in Chromium.')
  const bilal = { ...representativeUser, id: 2, user_key: 'user-two', uid: '8', user_id: '1008', display_name: 'Bilal Ahmed' }
  await page.route('**/api/v2/devices/connector-one/users*', async (route) => {
    const url = new URL(route.request().url())
    const rows = url.searchParams.get('q') === 'Bilal' ? [bilal] : [representativeUser, bilal]
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        rows,
        next_cursor: null,
        device,
        identity_integrity: { source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT', total_users: 2, with_cnic: 2, missing_cnic: 0, duplicate_groups: 0, duplicate_users: 0, resolved_duplicate_groups: 0, unresolved_duplicate_groups: 0, unresolved_duplicate_users: 0 },
      }),
    })
  })
  await page.goto('/users/connector-one')
  await page.getByLabel('Select Ayesha Khan for bulk deletion').check()
  await expect(page.getByText('1 user selected')).toBeVisible()

  await page.getByLabel('Search user name, user ID, or UID').fill('Bilal')
  await expect(page.getByRole('article', { name: /Ayesha Khan/i })).toHaveCount(0)
  await expect(page.getByRole('article', { name: /Bilal Ahmed/i })).toBeVisible()
  await expect(page.getByText('1 user selected')).toBeVisible()
  await expect(page.getByText(/0 selected of 1 eligible in view · 1 total/i)).toBeVisible()

  await page.getByLabel('Select Bilal Ahmed for bulk deletion').check()
  await expect(page.getByText('2 users selected')).toBeVisible()
  await page.getByRole('checkbox', { name: /Select eligible users in this view/i }).uncheck()
  await expect(page.getByText('1 user selected')).toBeVisible()

  await page.getByLabel('Search user name, user ID, or UID').fill('')
  await expect(page.getByLabel('Select Ayesha Khan for bulk deletion')).toBeChecked()
  await expect(page.getByLabel('Select Bilal Ahmed for bulk deletion')).not.toBeChecked()
})

test('source exception inspector is responsive, keyboard-operable, and fail-closed', async ({ page }) => {
  await page.goto('/reconciliation')
  await expect(page.getByRole('heading', { name: 'Terminal truth & recovery' })).toBeVisible()
  await page.getByRole('tab', { name: /Source exceptions/ }).click()
  await expect(page.getByRole('heading', { name: 'Immutable source exception ledger' })).toBeVisible()
  await expect(page.getByText('IMPLAUSIBLE_TERMINAL_TIME')).toBeVisible()
  const exceptionRow = page.getByRole('listitem').filter({ hasText: 'IMPLAUSIBLE_TERMINAL_TIME' })
  await exceptionRow.getByRole('button', { name: 'Inspect' }).focus()
  await page.keyboard.press('Enter')
  const drawer = page.getByRole('dialog', { name: 'Terminal source exception' })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByText('Excluded from attendance and Oracle')).toBeVisible()
  await drawer.getByLabel('Audited reason').fill('Inspect immutable source bytes for incident review.')
  await drawer.getByLabel('Administrator password').fill('not-recorded-by-test')
  await drawer.getByRole('button', { name: 'Reveal protected bytes' }).click()
  await expect(drawer.getByText('ffffffffffffffff')).toBeVisible()
  await drawer.getByRole('button', { name: 'Close dialog' }).click()
  await page.getByRole('button', { name: /SLICTOWER · 3rd Floor · ordinal 5,130/ }).click()
  const divergenceDrawer = page.getByRole('dialog', { name: 'Terminal-history change' })
  await expect(divergenceDrawer.getByText('Independent fresh-buffer verification')).toBeVisible()
  await expect(divergenceDrawer.getByText(/Old evidence remains immutable/)).toBeVisible()
  await divergenceDrawer.getByLabel('Audited reason').fill('Inspect preserved terminal mutation evidence.')
  await divergenceDrawer.getByLabel('Administrator password').fill('not-recorded-by-test')
  await divergenceDrawer.getByRole('button', { name: 'Reveal protected bytes' }).click()
  await expect(divergenceDrawer.getByText('ffffffffffffffff')).toBeVisible()
  const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
})

test('large reconciliation evidence stays viewport-bound and progressively reveals events', async ({ page }) => {
  await page.goto('/reconciliation')
  const inspect = page.getByRole('button', { name: 'Inspect evidence' }).first()
  await inspect.click()
  const drawer = page.getByRole('dialog', { name: 'Reconciliation evidence' })
  await expect(drawer).toBeVisible()
  await expect(drawer.locator('.reconciliation-event-summary')).toHaveCount(25)
  await expect(drawer.getByText('Showing 25 newest')).toBeVisible()

  const longStatusGeometry = await drawer.getByLabel('Status: SOURCE CAPTURE CERTIFIED WITH IMMUTABLE CHAIN EVIDENCE').first().evaluate((badge) => {
    const box = badge.getBoundingClientRect()
    const copy = badge.parentElement?.nextElementSibling?.getBoundingClientRect()
    const text = badge.querySelector('span')
    return {
      display: getComputedStyle(badge).display,
      flexDirection: getComputedStyle(badge).flexDirection,
      whiteSpace: text ? getComputedStyle(text).whiteSpace : '',
      nonOverlapping: Boolean(copy && (box.right <= copy.left || copy.right <= box.left || box.bottom <= copy.top || copy.bottom <= box.top)),
      box: { left: box.left, top: box.top, right: box.right, bottom: box.bottom },
      copy: copy ? { left: copy.left, top: copy.top, right: copy.right, bottom: copy.bottom } : null,
      title: badge.getAttribute('title'),
    }
  })
  expect(['flex', 'inline-flex']).toContain(longStatusGeometry.display)
  expect(longStatusGeometry.flexDirection).toBe('row')
  expect(longStatusGeometry.whiteSpace).toBe('nowrap')
  expect(longStatusGeometry.nonOverlapping, JSON.stringify(longStatusGeometry)).toBe(true)
  expect(longStatusGeometry.title).toBe('SOURCE CAPTURE CERTIFIED WITH IMMUTABLE CHAIN EVIDENCE')

  const geometry = await page.evaluate(() => {
    const viewport = window.visualViewport
    const backdrop = document.querySelector<HTMLElement>('.dialog-backdrop')?.getBoundingClientRect()
    const dialog = document.querySelector<HTMLElement>('.reconciliation-detail-drawer')?.getBoundingClientRect()
    const workspace = document.querySelector<HTMLElement>('.app-workspace')
    return {
      width: viewport?.width || window.innerWidth,
      height: viewport?.height || window.innerHeight,
      backdrop: backdrop && { x: backdrop.x, y: backdrop.y, right: backdrop.right, bottom: backdrop.bottom },
      dialog: dialog && { x: dialog.x, y: dialog.y, right: dialog.right, bottom: dialog.bottom },
      workspaceOverflow: workspace?.style.overflow,
      rootInert: document.getElementById('root')?.hasAttribute('inert'),
    }
  })
  expect(geometry.backdrop?.x).toBeGreaterThanOrEqual(-1)
  expect(geometry.backdrop?.y).toBeGreaterThanOrEqual(-1)
  expect(geometry.backdrop?.right).toBeLessThanOrEqual(geometry.width + 1)
  expect(geometry.backdrop?.bottom).toBeLessThanOrEqual(geometry.height + 1)
  expect(geometry.dialog?.x).toBeGreaterThanOrEqual(-1)
  expect(geometry.dialog?.y).toBeGreaterThanOrEqual(-1)
  expect(geometry.dialog?.right).toBeLessThanOrEqual(geometry.width + 1)
  expect(geometry.dialog?.bottom).toBeLessThanOrEqual(geometry.height + 1)
  expect(geometry.workspaceOverflow).toBe('hidden')
  expect(geometry.rootInert).toBe(true)

  await drawer.locator('.reconciliation-event-summary').first().click()
  await expect(drawer.locator('.reconciliation-event-ledger article pre')).toHaveCount(1)
  const expandedBox = await drawer.locator('.reconciliation-event-ledger article pre').boundingBox()
  const drawerBox = await drawer.boundingBox()
  expect((expandedBox?.x || 0) + (expandedBox?.width || 0)).toBeLessThanOrEqual((drawerBox?.x || 0) + (drawerBox?.width || 0) + 1)

  await drawer.getByRole('button', { name: 'Load 25 older events' }).click()
  await expect(drawer.locator('.reconciliation-event-summary')).toHaveCount(50)
  await drawer.getByRole('button', { name: 'Close dialog' }).click()
  await expect(inspect).toBeFocused()
  await expect(page.locator('#root')).not.toHaveAttribute('inert', '')
})

test('reported viewport thresholds, route scroll reset, and collision layers remain bounded', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-1280', 'Threshold matrix runs once in Chromium; Firefox and WebKit retain their dedicated projects.')
  const sizes = [
    { width: 320, height: 720 }, { width: 390, height: 844 },
    { width: 759, height: 800 }, { width: 760, height: 800 }, { width: 761, height: 800 },
    { width: 768, height: 1024 }, { width: 1024, height: 600 }, { width: 1224, height: 697 },
    { width: 1279, height: 720 }, { width: 1280, height: 720 }, { width: 1281, height: 720 },
    { width: 1440, height: 900 }, { width: 1600, height: 1000 },
  ]
  const routes = ['/fleet', '/users/connector-one', '/attendance', '/reconciliation', '/firmware', '/firmware?tab=prepare', '/alerts']
  for (let index = 0; index < sizes.length; index += 1) {
    await page.setViewportSize(sizes[index])
    await page.goto(routes[index % routes.length])
    await expect(page.locator('.app-header')).toBeVisible()
    await page.waitForTimeout(200)
    const bounds = await page.evaluate(() => ({
      width: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      shell: document.querySelector<HTMLElement>('.app-shell')?.getBoundingClientRect().height || 0,
      workspace: document.querySelector<HTMLElement>('.app-workspace')?.getBoundingClientRect().height || 0,
      viewportHeight: window.innerHeight,
      offenders: Array.from(document.querySelectorAll<HTMLElement>('body *')).filter((element) => {
        const rect = element.getBoundingClientRect()
        return rect.right > window.innerWidth + 1 || rect.left < -1
      }).slice(0, 8).map((element) => ({ tag: element.tagName, text: element.textContent?.trim().slice(0, 30), className: element.className, parent: element.parentElement?.className, right: Math.round(element.getBoundingClientRect().right), scrollWidth: element.scrollWidth })),
    }))
    expect(bounds.documentWidth, `${sizes[index].width}x${sizes[index].height} on ${routes[index % routes.length]} offenders=${JSON.stringify(bounds.offenders)}`).toBeLessThanOrEqual(bounds.width)
    expect(Math.abs(bounds.shell - bounds.viewportHeight)).toBeLessThanOrEqual(1)
    expect(Math.abs(bounds.workspace - bounds.viewportHeight)).toBeLessThanOrEqual(1)
  }

  await page.setViewportSize({ width: 1224, height: 697 })
  await page.goto('/users/connector-one')
  const workspace = page.locator('.app-workspace')
  await expect(page.getByRole('article', { name: /Ayesha Khan, user 1007/i })).toBeVisible()
  await page.waitForTimeout(120)
  await workspace.evaluate((node) => { node.scrollTop = Math.min(360, node.scrollHeight - node.clientHeight) })
  const savedScroll = await workspace.evaluate((node) => node.scrollTop)
  expect(savedScroll).toBeGreaterThan(0)
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Firmware', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Firmware operations' })).toBeVisible()
  await expect.poll(() => workspace.evaluate((node) => node.scrollTop)).toBe(0)
  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Device users' })).toBeVisible()
  await expect.poll(() => workspace.evaluate((node) => node.scrollTop)).toBe(savedScroll)

  await page.route('**/api/v1/devices*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: nationwideDevices }) }))
  await page.goto('/users')
  const pickerLayer = page.locator('.terminal-picker-layer')
  await expect(pickerLayer).toBeVisible()
  const finalOption = pickerLayer.getByRole('option').last()
  await finalOption.scrollIntoViewIfNeeded()
  const layerBox = await pickerLayer.boundingBox()
  const finalOptionBox = await finalOption.boundingBox()
  expect(layerBox?.x || 0).toBeGreaterThanOrEqual(11)
  expect((layerBox?.x || 0) + (layerBox?.width || 0)).toBeLessThanOrEqual(1224 - 11)
  expect(layerBox?.y || 0).toBeGreaterThanOrEqual(11)
  expect((layerBox?.y || 0) + (layerBox?.height || 0)).toBeLessThanOrEqual(697 - 11)
  expect(finalOptionBox?.y || 0).toBeGreaterThanOrEqual((layerBox?.y || 0) - 1)
  expect((finalOptionBox?.y || 0) + (finalOptionBox?.height || 0)).toBeLessThanOrEqual((layerBox?.y || 0) + (layerBox?.height || 0) + 1)
  await finalOption.click()
  await expect(page).toHaveURL(/\/users\/tower-thirteen$/)
  await expect(page.getByRole('button', { name: 'Change terminal' })).toBeVisible()

  const directoryUsers = Array.from({ length: 43 }, (_, index) => ({ ...representativeUser, id: index + 1, user_key: `operator-${index + 1}`, uid: `${index + 1}`, user_id: `${40_000 + index}`, display_name: `Operator ${index + 1}` }))
  await page.route('**/api/v2/devices/*/users*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: directoryUsers, next_cursor: null, device: nationwideDevices[4], identity_integrity: { source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT', total_users: 43, with_cnic: 43, missing_cnic: 0, duplicate_groups: 0, duplicate_users: 0, resolved_duplicate_groups: 0, unresolved_duplicate_groups: 0, unresolved_duplicate_users: 0 } }) }))
  await page.goto('/users/tower-three')
  const finalRow = page.getByRole('article', { name: /Operator 43, user 40042/i })
  await finalRow.scrollIntoViewIfNeeded()
  const finalMore = finalRow.getByLabel('More actions for Operator 43')
  await finalMore.click()
  const actionLayer = page.locator('.user-action-layer')
  await expect(actionLayer.getByRole('group', { name: 'Actions for Operator 43' })).toBeVisible()
  const actionBox = await actionLayer.boundingBox()
  expect(actionBox?.y || 0).toBeGreaterThanOrEqual(11)
  expect((actionBox?.y || 0) + (actionBox?.height || 0)).toBeLessThanOrEqual(697 - 11)
  await page.keyboard.press('Escape')
  await expect(finalMore).toBeFocused()

  const firstRow = page.getByRole('article', { name: /Operator 1, user 40000/i })
  await firstRow.getByRole('button', { name: 'Edit Operator 1' }).click()
  const userDialog = page.getByRole('dialog', { name: 'Edit device user' })
  await expect(userDialog).toBeVisible()
  const overlayOrder = await page.evaluate(() => {
    const overlayRoot = document.querySelector<HTMLElement>('#overlay-root')
    const stickyLayers = ['.app-header', '.users-section-tabs', '.user-directory-head']
      .map((selector) => document.querySelector<HTMLElement>(selector))
      .filter((node): node is HTMLElement => Boolean(node))
    const overlayZ = Number.parseInt(getComputedStyle(overlayRoot as HTMLElement).zIndex, 10)
    return {
      overlayZ,
      highestStickyZ: Math.max(...stickyLayers.map((node) => Number.parseInt(getComputedStyle(node).zIndex, 10) || 0)),
      dialogInsideOverlay: Boolean(document.querySelector('.dialog-backdrop')?.closest('#overlay-root')),
    }
  })
  expect(overlayOrder.dialogInsideOverlay).toBe(true)
  expect(overlayOrder.overlayZ).toBeGreaterThan(overlayOrder.highestStickyZ)
  await expect(page.locator('#root')).toHaveAttribute('inert', '')
  await page.getByRole('button', { name: 'Close dialog' }).click()
})

test('physical provisioning environment is responsive, explicit, and accessible', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'userAgent', {
      configurable: true,
      get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36',
    })
  })
  await page.goto('/firmware?tab=prepare')
  await expect(page.getByRole('heading', { name: 'Prepare a Zone Lite ESP32' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Connect the provisioning companion' })).toBeVisible()
  await expect(page.getByText('c'.repeat(64))).toBeVisible()
  await expect(page.getByText(/not OS code-signed/i)).toBeVisible()
  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    content: document.documentElement.scrollWidth,
    offenders: Array.from(document.querySelectorAll<HTMLElement>('body *'))
      .filter((element) => element.getBoundingClientRect().right > window.innerWidth + 1)
      .slice(0, 8)
      .map((element) => ({ tag: element.tagName, className: element.className, parent: element.parentElement?.className, right: Math.round(element.getBoundingClientRect().right), scrollWidth: element.scrollWidth })),
  }))
  expect(dimensions.content, JSON.stringify(dimensions.offenders)).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 })
})
