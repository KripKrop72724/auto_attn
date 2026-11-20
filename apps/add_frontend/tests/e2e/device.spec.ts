// One device: its panel and the attendance it keeps safe.
import { expect, test, device, mockDashboard } from './fixtures'

test.beforeEach(async ({ page }) => mockDashboard(page))

// The memory card sits in Summary › Technical details (04-device §2), closed by default.
async function openTechnicalDetails(page: import('@playwright/test').Page) {
  const dialog = page.getByRole('dialog')
  await dialog.locator('details[data-technical] > summary', { hasText: 'Technical details' }).first().click()
}

test('journal reader holds remain visible without granting writer permission', async ({ page }, testInfo) => {
  await page.route(/\/api\/v1\/devices(?:\/connector-one)?(?:\?.*)?$/, async route => {
    const reported = { ...device, boot_id: 'journal-test-boot', firmware_diagnostics_at: new Date().toISOString(),
      firmware_diagnostics: { schema_version: 2, boot_id: 'journal-test-boot', sampled_uptime_ms: 42000,
        runtime_profile: 'ZKT_JOURNAL_V1', delivery_authority: 'UNKNOWN',
        storage: { durability: 'HEALTHY', persistence_verified: true, recovery_complete: true },
        journal_storage: { observed: true, fresh: true, ready: true, durability: 'FULL',
          checkpoint_recovery_pending: false, sampled_uptime_ms: 41000, mailbox_capacity: 8,
          mailbox_high_watermark: 6, pending_appends: 0, last_append_result: 'FULL',
          last_failure_operation: 'storage_admission', last_filesystem_error: 28, last_nvs_error: 0 },
        workers: [{ name: 'storage_owner', state: 'WAITING_RESOURCE', operation: 'idle',
          pending_requests: 0, failures: 7, refusals: 2 }],
        queues: [{ name: 'legacy_migration', count_known: false, count_reason: 'UNVERIFIED_MIGRATION' }],
        journal_runtime: { observed: true, phase: 'READER_HOLD',
          reader_ready: true, writer_ready: false, start_attempts: 9, storage_starts: 1,
          delivery_starts: 1, capture_starts: 0, proof_attempts: 3, failures: 7,
          sampled_uptime_ms: 41000, compatibility: 'READER_COMPATIBILITY_EVIDENCE_REQUIRED' } } }
    await route.fulfill({ json: new URL(route.request().url()).pathname.endsWith('/connector-one') ? reported : { rows: [reported] } })
  })
  await page.goto('/devices/connector-one')
  await openTechnicalDetails(page)
  const preservation = page.getByRole('article', { name: 'Link box memory' })
  await expect(preservation.getByRole('heading', { name: 'Saving check-ins needs attention' })).toBeVisible()
  // The engineering rows sit under Technical details (ADV-18).
  await preservation.locator('summary', { hasText: 'Technical details' }).click()
  await expect(preservation.getByText('Reader compatibility needs review')).toBeVisible()
  await expect(preservation.getByText('1 storage · 1 delivery · 0 capture')).toBeVisible()
  await expect(preservation.getByText('Writer permission not confirmed')).toBeVisible()
  await expect(preservation.getByText('Local writer permitted')).toHaveCount(0)
  await expect(preservation.getByText(/Legacy custody verification pending/)).toBeVisible()
  await expect(preservation.getByText(/7 failures since boot/)).toBeVisible()
  const bounds = await preservation.evaluate(element => ({ width: element.clientWidth, scroll: element.scrollWidth }))
  expect(bounds.scroll).toBeLessThanOrEqual(bounds.width + 1)
  if (process.env.ADD_VISUAL_QA === '1') await preservation.screenshot({ path: testInfo.outputPath('journal-startup.png') })
})

test('capture timing keeps failures and boot identity visible', async ({ page }, testInfo) => {
  let wrongBoot = false
  await page.route(/\/api\/v1\/devices(?:\/connector-one)?(?:\?.*)?$/, async route => {
    const reported = { ...device, boot_id: 'capture-test-boot', firmware_diagnostics_at: new Date().toISOString(),
      firmware_diagnostics: { schema_version: 2, boot_id: wrongBoot ? 'previous-boot' : 'capture-test-boot',
        sampled_uptime_ms: 100000, delivery_authority: 'ADD',
        storage: { durability: 'FULL', persistence_verified: true, recovery_complete: false },
        workers: [{ name: 'capture', state: 'WAITING_RESOURCE', operation: 'idle',
          last_activity_uptime_ms: 99000, completed_operations: 100, failures: 3, timeouts: 1,
          packet_commit_latency_ms: { schema_version: 1, samples: 100, max_ms: 900, saturated: false,
            buckets: [0, 0, 0, 95, 0, 0, 0, 0, 4, 1, 0, 0, 0] },
          fragment_commit_latency_ms: { schema_version: 1, samples: 301, max_ms: 25, saturated: false,
            buckets: [0, 0, 0, 0, 301, 0, 0, 0, 0, 0, 0, 0, 0] } }], queues: [] } }
    await route.fulfill({ json: new URL(route.request().url()).pathname.endsWith('/connector-one') ? reported : { rows: [reported] } })
  })
  await page.goto('/devices/connector-one')
  await openTechnicalDetails(page)
  const preservation = page.getByRole('article', { name: 'Link box memory' })
  await expect(preservation.getByRole('heading', { name: 'Saving check-ins needs attention' })).toBeVisible()
  await preservation.locator('summary', { hasText: 'Technical details' }).click()
  await expect(preservation.getByText('p99 ≤ 500 ms · 100 complete samples')).toBeVisible()
  await expect(preservation.getByText('p99 ≤ 25 ms · 301 complete samples')).toBeVisible()
  await expect(preservation.getByText(/3 failures since boot · 1 timeout since boot/)).toBeVisible()
  await expect(preservation.getByText(/does not certify attendance delivery latency/)).toBeVisible()
  const bounds = await preservation.evaluate(element => ({ width: element.clientWidth, scroll: element.scrollWidth }))
  expect(bounds.scroll).toBeLessThanOrEqual(bounds.width + 1)
  if (process.env.ADD_VISUAL_QA === '1') {
    await preservation.getByText('p99 ≤ 500 ms · 100 complete samples').scrollIntoViewIfNeeded()
    await page.screenshot({ path: testInfo.outputPath('capture-timing.png') })
  }

  wrongBoot = true
  await page.reload()
  await openTechnicalDetails(page)
  await expect(preservation.getByRole('heading', { name: 'Waiting for a fresh report after a restart' })).toBeVisible()
  await expect(preservation.getByText('Current capture timing unverified')).toHaveCount(2)
  await expect(preservation.getByText(/p99 ≤/)).toHaveCount(0)
})

test('device header, five tabs and Activity fit every screen without sideways scroll', async ({ page }, testInfo) => {
  await page.goto('/devices/connector-one')
  const dialog = page.getByRole('dialog', { name: /3rd Floor/i })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('tab')).toHaveCount(5)
  await expect(dialog.getByText(/^Working/).first()).toBeVisible()
  const geometry = await page.evaluate(() => {
    const list = document.querySelector<HTMLElement>('.dv-tabs [role="tablist"]')
    const header = document.querySelector<HTMLElement>('.dv-header')
    return {
      listScroll: list?.scrollWidth ?? 0,
      listWidth: list?.clientWidth ?? 0,
      header: header?.getBoundingClientRect().height ?? 0,
      viewport: window.innerWidth,
      content: document.documentElement.scrollWidth,
    }
  })
  expect(geometry.listScroll).toBeLessThanOrEqual(geometry.listWidth + 1)
  expect(geometry.content).toBeLessThanOrEqual(geometry.viewport)
  // Plan budget 168 px at 375 px; this guards against the old squeezed, word-per-line header.
  if (geometry.viewport <= 390) expect(geometry.header).toBeLessThanOrEqual(200)
  await dialog.getByRole('tab', { name: 'Activity' }).click()
  await expect(dialog.getByRole('radiogroup', { name: 'Device activity' })).toBeVisible()
  await expect(page).toHaveURL(/tab=activity/)
  const activityWidth = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)
  expect(activityWidth).toBe(true)
  if (process.env.ADD_VISUAL_QA === '1') await page.screenshot({ path: testInfo.outputPath('device-activity.png') })
})
