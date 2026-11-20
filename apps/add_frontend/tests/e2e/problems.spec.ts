// Problems page (05-problems PRB-9): phone and desktop layout, tabs, filters in
// the link, Mark as fixed dialog, set-aside sheet. The shared fixture keeps the
// old answer shape (no counts, no plain words) to prove the fallback; these
// tests answer with the new fields.
import { expect, test, device, mockDashboard, expectOnlyAllowedStorage } from './fixtures'

const problemDevice = { ...device, division: 'health', friendly_name: 'SLIC Tower · 3rd Floor' }
const row = (id: number, overrides: Record<string, unknown> = {}) => ({
  id, code: 'ESP_DURABILITY_FAULT', severity: 'HIGH', state: 'OPEN',
  message: 'Attendance preservation needs recovery: storage DEGRADED.', details: {},
  first_seen_at: '2026-08-01T12:00:00Z', last_seen_at: '2026-08-01T17:00:00Z',
  acknowledged_at: null, acknowledged_by: null, resolved_at: null, resolution: null,
  plain_title: 'Link box cannot save attendance safely',
  plain_detail: 'Its memory has a problem, so check-ins could be lost if the power or internet goes off.',
  next_step: 'Do not switch the link box off. Call IT.',
  type: 'link_box_memory', affects_attendance: true,
  operator: { resolvable: true, refusal_code: null, refusal: null },
  device: { connector_id: device.connector_id, display_name: device.display_name, zone_id: device.zone_id, hardware_id: device.hardware_id, division: 'health', friendly_name: 'SLIC Tower · 3rd Floor' },
  ...overrides,
})
const rows = [
  row(1),
  row(2, { code: 'ATTENDANCE_EVENT_QUARANTINED', severity: 'WARNING', plain_title: '1 check-in set aside for checking', operator: { resolvable: false, refusal_code: 'ALERT_WORKFLOW_OWNED', refusal: 'Workflow.' } }),
]
const counts = {
  open: 2, seen: 0, legacy_seen: 0, fixed: 3,
  by_division: { life: { open: 0, seen: 0, fixed: 0 }, health: { open: 2, seen: 0, fixed: 3 } },
  by_severity: { CRITICAL: 0, HIGH: 1, WARNING: 1, MEDIUM: 0, INFO: 0 },
  by_type: { link_box_memory: 1, checkins: 1 }, legacy_by_code: {},
}

test.beforeEach(async ({ page }) => {
  await mockDashboard(page)
  await page.route(/\/api\/v1\/alerts(\?.*)?$/, (route) => route.fulfill({ json: { rows, next_cursor: null, totals: { all: 5, open: 2, acknowledged: 0, resolved: 3 }, counts } }))
  await page.route(/\/api\/v1\/devices(\?.*)?$/, (route) => route.fulfill({ json: { rows: [problemDevice] } }))
  await page.route(/\/api\/v1\/attendance-quarantine(\?.*)?$/, (route) => route.fulfill({ json: { totals: { all: 1, open: 1 }, filtered_total: 1, next_cursor: null, rows: [{
    id: 91, receipt_id: '11111111-2222-3333-4444-555555555555', connector_id: device.connector_id, device_id: device.device_id,
    display_name: device.display_name, zone_id: device.zone_id, item_index: 1, error_code: 'ATTENDANCE_EVENT_STRING_PATTERN_MISMATCH',
    error_path: 'event_uid', payload_digest: 'a'.repeat(64), review_state: 'OPEN', reviewed_by: null, review_reason: null,
    reviewed_at: null, observed_at: '2026-08-01T12:05:00Z', evidence_available: true, handling: 'QUARANTINED_NON_BLOCKING',
  }] } }))
})

const noSidewaysScroll = async (page: import('@playwright/test').Page) => {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
}

test('problems page fits, shows tabs with counts and keeps filters in the link', async ({ page }, testInfo) => {
  test.skip(!['mobile-390', 'desktop-1440'].includes(testInfo.project.name), 'Phone and desktop only.')
  await page.goto('/problems?tab=new&serious=important')
  await expect(page.getByRole('heading', { name: 'Problems', exact: true, level: 1 })).toBeVisible()
  const tabs = page.getByRole('tablist', { name: 'Problem status' })
  await expect(tabs.getByRole('tab', { name: 'New 2' })).toHaveAttribute('aria-selected', 'true')
  await expect(tabs.getByRole('tab', { name: 'Fixed 3' })).toBeVisible()
  const tabsFit = await tabs.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)
  expect(tabsFit).toBe(true)
  const card = page.getByRole('article', { name: 'Link box cannot save attendance safely' })
  await expect(card).toBeVisible()
  await expect(page.getByRole('button', { name: /Important/ })).toHaveAttribute('aria-pressed', 'true')
  await noSidewaysScroll(page)
  // Touch targets on the phone.
  if (testInfo.project.name === 'mobile-390') {
    const small = await page.locator('.problem-card button:not(.ui-icon-button):visible, .problem-card a:visible, .problem-group-toggle:visible, .problem-open-device:visible, .ui-tab:visible').evaluateAll((elements) =>
      elements.map((element) => element.getBoundingClientRect()).filter((box) => box.height > 0 && box.height < 43.5).length)
    expect(small).toBe(0)
  }
  await page.getByRole('tab', { name: 'Fixed 3' }).click()
  await expect(page).toHaveURL(/tab=fixed/)
  expect(page.url()).not.toMatch(/cnic|password|reason|confirmation/i)
  await expectOnlyAllowedStorage(page)
  if (process.env.ADD_VISUAL_QA === '1') await page.screenshot({ path: testInfo.outputPath('problems.png'), fullPage: true })
})

test('mark as fixed keeps the reason and the password; set-aside check-ins open from the card', async ({ page }, testInfo) => {
  test.skip(!['mobile-390', 'desktop-1440'].includes(testInfo.project.name), 'Phone and desktop only.')
  await page.goto('/problems')
  const card = page.getByRole('article', { name: 'Link box cannot save attendance safely' })
  if (testInfo.project.name === 'mobile-390') {
    await card.getByRole('button', { name: /More actions/ }).click()
    await page.getByRole('menuitem', { name: 'Mark as fixed…' }).click()
  } else {
    await card.getByRole('button', { name: /Mark as fixed/ }).click()
  }
  const dialog = page.getByRole('dialog', { name: 'Mark this problem as fixed' })
  await expect(dialog.getByLabel('What did you do?')).toBeVisible()
  await expect(dialog.getByLabel('Your password')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Mark as fixed' })).toBeDisabled()
  await noSidewaysScroll(page)
  await dialog.getByRole('button', { name: 'Cancel' }).click()

  await page.getByRole('article', { name: '1 check-in set aside for checking' }).getByRole('button', { name: /Check them/ }).click()
  const sheet = page.getByRole('dialog', { name: 'Check-ins set aside for checking' })
  await expect(sheet.getByText('A value in the record could not be read')).toBeVisible()
  await sheet.getByRole('button', { name: 'Check' }).click()
  await expect(sheet.getByRole('heading', { name: 'Check a set-aside check-in' })).toBeVisible()
  await expect(sheet.getByRole('button', { name: 'Show original record' })).toBeDisabled()
  await noSidewaysScroll(page)
})
