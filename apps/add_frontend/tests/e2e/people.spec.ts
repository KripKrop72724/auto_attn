// People on a machine.
import AxeBuilder from '@axe-core/playwright'
import { expect, test, device, expectOnlyAllowedStorage, mockDashboard, representativeUser } from './fixtures'

test.beforeEach(async ({ page }) => mockDashboard(page))

test('populated Users and Attendance workspaces are responsive, keyboard-operable, and accessible', async ({ page }) => {
  await page.goto('/people/connector-one')
  await expect(page.getByRole('heading', { name: 'Users', exact: true, level: 1 })).toBeVisible()
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
