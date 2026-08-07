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
    capabilities: { user_write: true, create_user: true, delete_user: true }, snapshot_complete: true,
    writes_disabled_reason: null, user_count: 1, attendance_count: 42, device_time: '2026-08-01T18:00:00Z',
    device_time_sampled_at: '2026-08-01T18:00:00Z', drift_seconds: 0, last_reconcile_at: '2026-08-01T17:50:00Z', next_restart_at: null,
  },
}

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

const reconciliationJob = {
  job_id: '11111111-1111-4111-8111-111111111111', mode: 'FULL_HISTORY_BASELINE', status: 'RUNNING', phase: 'VERIFYING_SOURCE_CHANGE', wait_reason: 'SOURCE_DIVERGENCE_PROBE_PENDING', error_code: null, error_message: null,
  operator_state: 'VERIFYING_SOURCE_CHANGE', operator_message: 'ADD is confirming a terminal-history change with independent fresh reads.', completion_outcome: null, review_required: false,
  connector: { connector_id: device.connector_id, device_id: device.device_id, display_name: device.display_name, zone_id: device.zone_id, connected: true },
  terminal: { serial: device.zkt.serial, generation: 2, cutoff_count: 5131, latest_count: 5131, record_size: 40, source_total_bytes: 205244 },
  progress: { scanned: 5100, remaining: 31, add_durable: 5100, already_present: 5098, terminal_duplicates: 0, blocked_identity: 172, quarantined: 1, oracle_target: 0, oracle_confirmed: 0, oracle_pending: 0, retry_count: 1, auto_retry_count: 1 },
  assignment: { assignment_id: null, credit_start_ordinal: null, credit_end_ordinal: null, credit_committed_through: 5100, granted_at: null, expires_at: null, accepted_at: null, heartbeat_at: null },
  eta: { low_seconds: null, high_seconds: null, confidence: 'UNAVAILABLE', unavailable_reason: 'SOURCE_DIVERGENCE_PROBE_PENDING' },
  recovery: { operation_id: '22222222-2222-4222-8222-222222222222', source_epoch: 1, source_epoch_id: '33333333-3333-4333-8333-333333333333', divergence: { divergence_id: '44444444-4444-4444-8444-444444444444', ordinal: 5130, state: 'PROBING', old_raw_digest: 'a'.repeat(64), new_raw_digest: 'b'.repeat(64), observation_count: 2, next_probe_at: '2026-08-07T06:00:00Z' } },
  requested_at: '2026-08-07T05:01:32Z', started_at: '2026-08-07T05:01:36Z', capture_certified_at: null, oracle_certified_at: null, completed_at: null, updated_at: '2026-08-07T05:35:05Z',
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
    else if (url.pathname.endsWith('/alerts')) json = { rows: [{ id: 1, code: 'ZKT_CLOCK_DRIFT', severity: 'WARNING', state: 'OPEN', message: 'Terminal clock requires review.', details: {}, first_seen_at: '2026-08-01T12:00:00Z', last_seen_at: '2026-08-01T17:00:00Z', acknowledged_at: null, resolved_at: null }] }
    else if (url.pathname === '/api/v1/attendance') json = { rows: [], next_cursor: null }
    else if (url.pathname === '/api/v1/firmware/releases') json = { enabled: true, hil_enabled: false, rows: [{ release_id: 'release-2.2.30', version: '2.2.30', git_sha: 'a'.repeat(40), image_sha256: 'b'.repeat(64), application_sha256: 'c'.repeat(64), image_size: 1024, state: 'AVAILABLE', partition_layout: 'ota-v2', signing_key_id: 'production-key', published_at: '2026-07-30T12:00:00Z', hil_target_mac: null }] }
    else if (url.pathname === '/api/v1/firmware/campaigns') json = { enabled: true, hil_enabled: false, rows: [] }
    else if (url.pathname === '/api/v1/reconciliations') json = { enabled: true, scheduler: { policy: 'BOUNDED_PARALLEL_PER_DEVICE', device_concurrency: 6, active_scan_jobs: 1, waiting_scan_jobs: 0, available_scan_slots: 5, history_backlog: 0, history_backlog_limit: 10000, reserved_credit: 0, available_credit: 10000 }, rows: [reconciliationJob] }
    else if (url.pathname.endsWith('/reconciliations/preflight')) json = { eligible: true, ready_now: true, hard_blockers: [], waitable_blockers: [], connector: { connector_id: device.connector_id, device_id: device.device_id, display_name: device.display_name, zone_id: device.zone_id, connected: true, firmware_version: device.firmware_version }, terminal: { serial: device.zkt.serial, attendance_count: 42, user_count: 1, connection_state: 'ONLINE', identity_snapshot_revision: 1, range_resume_verified: true }, coverage: null }
    else if (url.pathname === '/api/v1/source-exceptions/1/review') json = { ...sourceException, review_state: 'REVIEWED', reviewed_at: '2026-08-06T10:10:00Z', reviewed_by: 'StateHealthAdmin', review_reason: 'Reviewed immutable source evidence.', reviews: [{ review_id: 'review-one', state: 'REVIEWED', reason: 'Reviewed immutable source evidence.', actor: 'StateHealthAdmin', created_at: '2026-08-06T10:10:00Z' }] }
    else if (url.pathname === '/api/v1/source-exceptions/1/reveal') json = { id: 1, raw_record_b64: '//////////8=', raw_record_hex: 'ffffffffffffffff', raw_record_digest: sourceException.raw_record_digest, record_size: 40 }
    else if (url.pathname === '/api/v1/source-exceptions/1') json = sourceException
    else if (url.pathname === '/api/v1/source-exceptions') json = { totals: { all: 1, open: 1, reviewed: 0, invalid_time: 1, malformed: 0, affected_terminals: 1 }, rows: [sourceException], next_cursor: null }
    else if (url.pathname === '/api/v1/reconciliation-divergences/44444444-4444-4444-8444-444444444444/reveal') json = { divergence_id: '44444444-4444-4444-8444-444444444444', raw_record_b64: '//////////8=', raw_record_hex: 'ffffffffffffffff', new_raw_digest: 'b'.repeat(64) }
    else if (url.pathname === '/api/v1/reconciliation-divergences/44444444-4444-4444-8444-444444444444') json = { divergence_id: '44444444-4444-4444-8444-444444444444', job_id: reconciliationJob.job_id, ordinal: 5130, state: 'PROBING', old_raw_digest: 'a'.repeat(64), new_raw_digest: 'b'.repeat(64), old_disposition: 'INVALID_TIME', new_disposition: 'INVALID_TIME', observations: [{ raw_record_digest: 'b'.repeat(64), disposition: 'INVALID_TIME', observed_at: '2026-08-07T05:26:06Z', kind: 'RECONCILIATION_CHUNK' }, { raw_record_digest: 'b'.repeat(64), disposition: 'INVALID_TIME', observed_at: '2026-08-07T05:27:06Z', kind: 'FRESH_SOURCE_PROBE' }], evidence_available: true, created_at: '2026-08-07T05:26:06Z', resolved_at: null }
    else if (url.pathname.includes('/historical-identities')) json = { totals: { unresolved_events: 0, blocked_identity: 0, quarantined_identity_reuse: 0, unassigned_events: 0, actionable_event_groups: 0 }, rows: [], unassigned_groups: [] }
    else if (url.pathname.includes('/identity-conflicts')) json = { raw_duplicate_groups: 0, resolved_groups: 0, unresolved_groups: 0, groups: [], evidence_scope: { add_attendance_count: 0, terminal_attendance_count: 0, attendance_coverage_percent: 0 } }
    else if (url.pathname.includes('/user-deletion-jobs/latest')) json = { job: null }
    else if (url.pathname.includes('/users')) json = { rows: [], next_cursor: null, identity_integrity: { source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT', total_users: 0, with_cnic: 0, missing_cnic: 0, duplicate_groups: 0, duplicate_users: 0, resolved_duplicate_groups: 0, unresolved_duplicate_groups: 0, unresolved_duplicate_users: 0 } }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(json) })
  })
}

test.beforeEach(async ({ page }) => mockDashboard(page))

test('adaptive shell has no horizontal overflow and meets critical accessibility checks', async ({ page }) => {
  await page.goto('/fleet')
  await expect(page.getByRole('heading', { name: 'Attendance device command center' })).toBeVisible()
  const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
})

test('primary routes and device deep link remain usable', async ({ page }) => {
  await page.goto('/fleet')
  await page.getByRole('button', { name: 'Inspect SLICTOWER · 3rd Floor' }).click()
  await expect(page).toHaveURL(/\/fleet\/connector-one$/)
  await expect(page.getByRole('dialog', { name: 'SLICTOWER · 3rd Floor' })).toBeVisible()
  await page.getByRole('button', { name: 'Close dialog' }).click()

  const primaryNav = page.getByRole('navigation', { name: 'Primary navigation' })
  await primaryNav.getByRole('button', { name: 'Users', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Device users' })).toBeVisible()
  await primaryNav.getByRole('button', { name: 'Attendance', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Attendance events' })).toBeVisible()
  await primaryNav.getByRole('button', { name: 'Firmware', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Firmware operations' })).toBeVisible()
  await primaryNav.getByRole('button', { name: /Alerts/ }).click()
  await expect(page.getByRole('heading', { name: 'Alerts and exceptions' })).toBeVisible()
  expect(page.url()).not.toMatch(/cnic|password|reason|confirmation/i)
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 })
})

test('source exception inspector is responsive, keyboard-operable, and fail-closed', async ({ page }) => {
  await page.goto('/reconciliation')
  await expect(page.getByRole('heading', { name: 'Start-of-time reconciliation' })).toBeVisible()
  await page.getByRole('tab', { name: /Source exceptions/ }).click()
  await expect(page.getByRole('heading', { name: 'Terminal source exceptions' })).toBeVisible()
  await expect(page.getByText('IMPLAUSIBLE_TERMINAL_TIME')).toBeVisible()
  await page.getByRole('button', { name: /Inspect source exception ordinal 5043/ }).focus()
  await page.keyboard.press('Enter')
  const drawer = page.getByRole('dialog', { name: 'Terminal source exception' })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByText('Excluded from attendance and Oracle')).toBeVisible()
  await expect(drawer.getByText(/valid punches after this row can continue/)).toBeVisible()
  await drawer.getByLabel('Audited reason').fill('Inspect immutable source bytes for incident review.')
  await drawer.getByLabel('Administrator password').fill('not-recorded-by-test')
  await drawer.getByRole('button', { name: 'Reveal raw evidence' }).click()
  await expect(drawer.getByText('ffffffffffffffff')).toBeVisible()
  await drawer.getByRole('button', { name: 'Close dialog' }).click()
  await page.getByRole('button', { name: /SLICTOWER · 3rd Floor · ordinal 5,130/ }).click()
  const divergenceDrawer = page.getByRole('dialog', { name: 'Terminal-history change' })
  await expect(divergenceDrawer.getByText('Independent fresh-buffer verification')).toBeVisible()
  await expect(divergenceDrawer.getByText(/Old evidence remains immutable/)).toBeVisible()
  await divergenceDrawer.getByLabel('Audited reason').fill('Inspect preserved terminal mutation evidence.')
  await divergenceDrawer.getByLabel('Administrator password').fill('not-recorded-by-test')
  await divergenceDrawer.getByRole('button', { name: 'Reveal raw evidence' }).click()
  await expect(divergenceDrawer.getByText('ffffffffffffffff')).toBeVisible()
  const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
})
