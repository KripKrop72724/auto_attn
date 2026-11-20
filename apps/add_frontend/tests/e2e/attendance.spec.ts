// Attendance: check-ins, fix tools and releases.
import AxeBuilder from '@axe-core/playwright'
import { expect, test, device, mockDashboard, representativeAttendance } from './fixtures'

test.beforeEach(async ({ page }) => mockDashboard(page))

test('Attendance combines Oracle filters and offers an accessible per-punch send review', async ({ page }) => {
  await page.route('**/api/v1/attendance?*', async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({
      rows: [{ ...representativeAttendance, ords_status: 'BLOCKED_IDENTITY', oracle_confirmed_at: null }],
      next_cursor: null, status_options: ['BLOCKED_IDENTITY', 'FAILED_RETRYABLE'],
    }),
  }))
  await page.goto('/attendance')
  const row = page.getByRole('article', { name: /Ayesha Khan, Check in/i })
  await expect(row).toBeVisible()
  await page.locator('.attendance-advanced > summary').click()
  await page.getByRole('checkbox', { name: 'Blocked identity' }).check()
  await page.getByRole('checkbox', { name: 'Failed retryable' }).check()
  await page.getByLabel('CNIC availability').selectOption('present')
  await page.getByRole('button', { name: 'View results' }).click()
  await expect(page.getByRole('button', { name: /Oracle: BLOCKED IDENTITY/i })).toBeVisible()
  await expect(page.getByRole('button', { name: /Oracle: FAILED RETRYABLE/i })).toBeVisible()
  await row.getByRole('button', { name: 'Send to ORDS' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('heading', { name: 'Send 1 punch to Oracle' })).toBeVisible()
  await expect(dialog.getByLabel('Administrator password')).toBeVisible()
  const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).include('[role="dialog"]').withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze()
  expect(results.violations).toEqual([])
})

test('legacy employee repair route opens the responsive blocked-punch review', async ({ page }, testInfo) => {
  await page.goto('/reconciliation?tab=employee-repair&device_id=connector-one')
  await expect(page).toHaveURL(/\/attendance\?view=needs-review&device_id=connector-one$/)
  await expect(page.getByRole('heading', { name: 'Attendance', exact: true, level: 1 })).toBeVisible()
  await expect(page.getByRole('tab', { name: /Needs review/ })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('heading', { name: 'Force release attendance' })).toBeVisible()
  await expect(page.getByText('Manual approval only')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Sync and check' })).toBeEnabled()
  await expect(page.getByRole('button', { name: 'Review punches' })).toHaveCount(0)

  // Enabling this control starts its disabled-to-enabled color transition.
  // Inspect the final rendered colors without weakening the contrast check.
  await page.getByRole('button', { name: 'Sync and check' }).evaluate(async (node) => {
    await Promise.all(node.getAnimations().map(animation => animation.finished.catch(() => undefined)))
  })
  const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
  if (process.env.ADD_VISUAL_QA === '1') await page.screenshot({ path: testInfo.outputPath('attendance-release-review.png'), fullPage: true })
})

test('manual force release remains clear, accessible and resumable', async ({ page }, testInfo) => {
  let run = {
    job_id: '11111111-1111-4111-8111-111111111111', status: 'CHECKED', actor: 'StateHealthAdmin',
    counts: { checked: 328, ready: 300, waiting: 0, confirmed: 0, review: 28, stopped: 0, skipped: 0, already_confirmed: 0, already_delivering: 0, unavailable_devices: 0 },
    check_complete: true, created_at: '2026-09-22T10:00:00Z', updated_at: '2026-09-22T10:00:00Z',
    expires_at: '2099-09-22T10:15:00Z', signature: 'a'.repeat(64), last_error: null,
    execution_enabled: true, eligible_at_check: 300,
    devices: [{ connector_id: device.connector_id, name: device.display_name, serial: device.zkt.serial, checked: 328, ready: 300, status: 'CHECKED', synced_at: '2026-09-23T10:00:00Z', error: null }],
  }
  await page.route(/\/api\/v2\/attendance-force-releases(?:\/|\?|$)/, async route => {
    const path = new URL(route.request().url()).pathname
    let json: unknown = run
    if (path.endsWith('/attendance-force-releases') && route.request().method() === 'GET') json = { enabled: true, rows: [run], next_cursor: null }
    if (path.endsWith('/coverage')) json = { total: 2576, confirmed: 2248, pending: 0, identity_held: 328, review: 0, observed_at: run.updated_at }
    if (path.endsWith('/items')) json = { rows: [{ id: 1, event_id: 1, event_uid: 'e'.repeat(64), status: 'NEEDS_REVIEW', reason: 'We need proof of who used this employee number when the punch was made.', name: 'Example employee', user_id: '100', connector_id: device.connector_id, device_name: device.display_name, device_serial: device.zkt.serial, time: run.created_at }], next_cursor: null }
    if (path.endsWith('/start') && route.request().method() === 'POST') {
      expect(route.request().postDataJSON()).toMatchObject({ reason: 'Current terminal identity checked', signature: run.signature, password: 'test-password' })
      run = { ...run, status: 'WAITING_ORACLE', counts: { ...run.counts, ready: 0, waiting: 300 } }
      json = run
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(json) })
  })
  await page.goto(`/attendance?view=needs-review&force_run=${run.job_id}`)
  await expect(page.getByRole('heading', { name: 'Ready to review' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Force release 300 punches', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: 'Force release 300 punches', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: 'Close dialog' })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(dialog.getByLabel('Reason for release')).toBeFocused()
  await dialog.getByLabel('Reason for release').fill('Current terminal identity checked')
  await dialog.getByLabel('Administrator password').fill('test-password')
  await dialog.getByRole('button', { name: 'Approve 300 punches', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Waiting for Oracle' })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Waiting for Oracle' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Finished', exact: true })).toHaveCount(0)
  await expect(page.locator('.safe-repair-steps [aria-current="step"]')).toHaveText('Waiting for Oracle')
  // Inspect final rendered colors after the shared route entrance animation.
  await page.evaluate(async () => {
    await Promise.all(document.getAnimations().filter(animation => animation.effect?.getComputedTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {})))
  })
  const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).include('.safe-repair').withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze()
  expect(results.violations).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('attendance-repair.png'), fullPage: true })
})
