// Attendance: check-ins, fix tools and releases.
import AxeBuilder from '@axe-core/playwright'
import { expect, test, device, mockDashboard, representativeAttendance } from './fixtures'

test.beforeEach(async ({ page }) => mockDashboard(page))

test('Attendance filters by status, keeps them in the address and offers an accessible send from the sheet', async ({ page }) => {
  await page.route('**/api/v1/attendance?*', async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({
      rows: [{
        ...representativeAttendance, ords_status: 'BLOCKED_IDENTITY', delivery_group: 'not_matched', oracle_confirmed_at: null,
        source_connector: { connector_id: device.connector_id, display_name: device.zone_id, zone_id: device.zone_id, zone_name: device.zone_name, friendly_name: 'SLIC Tower · 3rd Floor', division: 'health' },
        direct_ords_identity: { eligible: true, cnic_source: 'SAVED_PUNCH', exclusion: null },
      }],
      next_cursor: null,
    }),
  }))
  await page.goto('/attendance')
  await expect(page.getByRole('heading', { name: 'Attendance', exact: true, level: 1 })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Check-ins' })).toHaveAttribute('aria-selected', 'true')
  const row = page.getByRole('article', { name: /Ayesha Khan, Check-in/i })
  await expect(row).toBeVisible()
  const phone = (page.viewportSize()?.width || 0) <= 760
  if (phone) {
    await page.getByRole('button', { name: /More filters/ }).click()
    await page.getByRole('checkbox', { name: /^Not matched/ }).check()
    await page.getByRole('checkbox', { name: /^Waiting/ }).check()
    await page.getByRole('switch', { name: 'No CNIC' }).check()
    await page.getByRole('button', { name: 'Show results' }).click()
  } else {
    await page.getByRole('button', { name: /^Status:/ }).click()
    await page.getByRole('checkbox', { name: /^Not matched/ }).check()
    await page.getByRole('checkbox', { name: /^Waiting/ }).check()
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: /More filters/ }).click()
    await page.getByRole('switch', { name: 'No CNIC' }).check()
    await page.keyboard.press('Escape')
  }
  await expect(page.getByRole('button', { name: 'Remove filter: Not matched' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Remove filter: Waiting' })).toBeVisible()
  await expect(page).toHaveURL(/status=not_matched%2Cwaiting/)
  await expect(page).not.toHaveURL(/cnic|password|reason|confirmation/i)
  await row.getByRole('button', { name: /^Open check-in/ }).click()
  const sheet = page.getByRole('dialog', { name: 'Ayesha Khan' })
  await expect(sheet.getByText('Journey')).toBeVisible()
  await sheet.getByRole('button', { name: 'Send to HR system' }).click()
  const dialog = page.getByRole('dialog', { name: /Send 1 check-in to the HR system \(Oracle\)/ })
  await expect(dialog.getByRole('heading', { name: 'Send 1 check-in to the HR system (Oracle)' })).toBeVisible()
  await expect(dialog.getByLabel('Your password')).toBeVisible()
  const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).include('[role="dialog"]').withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze()
  expect(results.violations).toEqual([])
})

test('Attendance list is not clipped and the first check-in is near the top on a phone', async ({ page }) => {
  await page.goto('/attendance')
  const row = page.getByRole('article', { name: /Ayesha Khan, Check-in/i })
  await expect(row).toBeVisible()
  const list = await page.locator('.attendance-list').evaluate((element) => ({ scroll: element.scrollWidth, width: element.clientWidth }))
  expect(list.scroll).toBeLessThanOrEqual(list.width + 1)
  if ((page.viewportSize()?.width || 0) <= 760) {
    // Plan target is about 370 px under the shell's compact header; this bound catches a regression
    // to the old ~1,200 px stack until the shell's phone header lands (see the PR notes).
    const box = await row.boundingBox()
    expect(box?.y || 0).toBeLessThanOrEqual(640)
  }
  await expect(page.getByText(/Phone notifications|phone messages/i)).toHaveCount(0)
})

test('legacy employee repair route opens the responsive blocked-punch review', async ({ page }, testInfo) => {
  await page.goto('/reconciliation?tab=employee-repair&device_id=connector-one')
  // Reconciliation still sends the old link; Attendance rewrites it to Fix tools.
  await expect(page).toHaveURL(/\/attendance\?view=fix-tools&device_id=connector-one&tool=held-staff$/)
  await expect(page.getByRole('heading', { name: 'Attendance', exact: true, level: 1 })).toBeVisible()
  await expect(page.getByRole('tab', { name: /Fix tools/ })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('heading', { name: 'Re-check not-matched check-ins' })).toBeVisible()
  await expect(page.getByText('Needs your approval')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Re-check not-matched check-ins' })).toBeEnabled()
  await expect(page.getByRole('button', { name: 'Review punches' })).toHaveCount(0)

  // Enabling this control starts its disabled-to-enabled color transition.
  // Inspect the final rendered colors without weakening the contrast check.
  await page.getByRole('button', { name: 'Re-check not-matched check-ins' }).evaluate(async (node) => {
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
  // The old link keeps working: it is rewritten to Fix tools › Re-check not-matched check-ins.
  await page.goto(`/attendance?view=needs-review&force_run=${run.job_id}`)
  await expect(page).toHaveURL(new RegExp(`view=fix-tools&force_run=${run.job_id}&tool=held-staff$`))
  await expect(page.getByRole('heading', { name: 'Ready to send' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Send 300 check-ins', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: 'Send 300 check-ins', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: 'Close', exact: true })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(dialog.getByLabel('Why?', { exact: true })).toBeFocused()
  await dialog.getByLabel('Why?', { exact: true }).fill('Current terminal identity checked')
  await dialog.getByLabel('Your password').fill('test-password')
  await dialog.getByRole('button', { name: 'Send 300 check-ins', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Waiting for the HR system' })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Waiting for the HR system' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Done', exact: true })).toHaveCount(0)
  await expect(page.locator('.safe-repair-steps [aria-current="step"]')).toHaveText('Waiting for HR system')
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
