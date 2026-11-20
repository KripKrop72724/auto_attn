// People on a machine.
import AxeBuilder from '@axe-core/playwright'
import { expect, test, device, expectOnlyAllowedStorage, mockDashboard, representativeUser } from './fixtures'

test.beforeEach(async ({ page }) => mockDashboard(page))

test('populated People and Attendance workspaces are responsive, keyboard-operable, and accessible', async ({ page }) => {
  await page.goto('/people/connector-one')
  await expect(page.getByRole('heading', { name: 'People', exact: true, level: 1 })).toBeVisible()
  const row = page.getByRole('article', { name: /Ayesha Khan, Employee ID 1007/i })
  await expect(row).toBeVisible()
  await expect(page.getByRole('tab', { name: /People/ })).toHaveAttribute('aria-selected', 'true')
  const phone = (page.viewportSize()?.width || 0) <= 760
  if (phone) {
    // Phone: the card opens the person sheet; Escape gives focus back to the card.
    const open = row.locator('.people-person-open')
    await open.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('dialog', { name: 'Ayesha Khan' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(open).toBeFocused()
  } else {
    const more = page.getByLabel('More for Ayesha Khan')
    await more.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('group', { name: 'Options for Ayesha Khan' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(more).toBeFocused()
  }
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
  await expectOnlyAllowedStorage(page)
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
  await page.goto('/people/connector-one')
  await page.getByLabel('Select Ayesha Khan').check()
  await expect(page.getByText('1 selected', { exact: true })).toBeVisible()

  await page.getByLabel('Search name, Employee ID or CNIC').fill('Bilal')
  await expect(page.getByRole('article', { name: /Ayesha Khan/i })).toHaveCount(0)
  await expect(page.getByRole('article', { name: /Bilal Ahmed/i })).toBeVisible()
  await expect(page.getByText('1 selected', { exact: true })).toBeVisible()
  await expect(page.getByText(/0 of 1 on this page selected · 1 selected in total/i)).toBeVisible()

  await page.getByLabel('Select Bilal Ahmed').check()
  await expect(page.getByText('2 selected', { exact: true })).toBeVisible()
  await page.getByRole('checkbox', { name: 'Select all on this page' }).uncheck()
  await expect(page.getByText('1 selected', { exact: true })).toBeVisible()

  await page.getByLabel('Search name, Employee ID or CNIC').fill('')
  await expect(page.getByLabel('Select Ayesha Khan')).toBeChecked()
  await expect(page.getByLabel('Select Bilal Ahmed')).not.toBeChecked()
})

test('the device picker lists every device at once and opens one', async ({ page }) => {
  await page.goto('/people')
  await expect(page.getByRole('heading', { name: 'People', exact: true, level: 1 })).toBeVisible()
  await expect(page.getByText(/No device matches/)).toHaveCount(0)
  const first = page.locator('.people-device-list a.ui-row-link').first()
  await expect(first).toBeVisible()
  await first.click()
  await expect(page).toHaveURL(/\/people\/[^/?]+/)
  await expect(page.getByRole('tab', { name: /People/ })).toHaveAttribute('aria-selected', 'true')
  expect(page.url()).not.toMatch(/cnic|password|reason|confirmation/i)
})
