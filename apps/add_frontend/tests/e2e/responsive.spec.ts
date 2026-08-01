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
