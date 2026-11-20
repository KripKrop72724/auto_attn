// Home and the Devices list (03-home-devices): Life and Health, tiles that
// equal the list, filters in the address, phone cards that never cut a name.
import AxeBuilder from '@axe-core/playwright'
import { expect, test, mockDashboard, nationwideDevices } from './fixtures'
import { reportPlainWords } from './plainWords'

test.beforeEach(async ({ page }) => {
  await mockDashboard(page)
  await page.route('**/api/v1/devices*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ rows: nationwideDevices }),
  }))
})

test('Home splits Life and Health and Devices filters by the page address', async ({ page }, testInfo) => {
  await page.goto('/home')
  await expect(page.getByRole('heading', { name: 'Home', exact: true, level: 1 })).toBeVisible()
  const summary = page.getByRole('region', { name: 'Summary' })
  await expect(summary.getByRole('link').first()).toContainText('13')
  await expect(summary.getByRole('link').nth(2)).toContainText('2')
  await expect(summary.getByRole('link').nth(3)).toContainText('1')
  const life = page.getByRole('region', { name: 'Life' })
  const health = page.getByRole('region', { name: 'Health' })
  await expect(life.getByRole('link', { name: /^Open Building 9 · Device 3, / })).toBeVisible()
  await expect(health.getByRole('link', { name: /^Open Multan · Device 2, / })).toBeVisible()
  await expect(health.getByRole('link', { name: /^Open Peshawar · Device 2, / })).toBeVisible()
  await expect(health.getByRole('link', { name: /^Open Swat(,|$)/ })).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Attendance today' })).toContainText('1,038')
  await reportPlainWords(page, testInfo, '/home')

  // Needs attention tile → the filtered list, in the address.
  await summary.getByRole('link').nth(2).click()
  await expect(page).toHaveURL(/\/devices\?status=attention$/)
  await expect(page.getByRole('link', { name: /^Open .*needs attention$/ })).toHaveCount(2)
  await expect(page.getByText('Showing 2 of 13')).toBeVisible()

  // The Division switch narrows every count and keeps the filter.
  const phone = (page.viewportSize()?.width || 0) <= 760
  await page.locator(phone ? '.phone-division-row' : '.header-division').getByRole('radio', { name: 'Life' }).click()
  await expect(page).toHaveURL(/status=attention/)
  await expect(page).toHaveURL(/division=life/)
  await expect(page.getByRole('link', { name: /^Open / })).toHaveCount(1)
  await expect(page.getByText('Showing Life only')).toBeVisible()

  // A shared address opens the same view.
  await page.goto('/devices?q=karachi')
  await expect(page.getByRole('link', { name: /^Open Building 9 · Device [13]/ })).toHaveCount(2)
  await page.goto('/devices?status=offline')
  await expect(page.getByRole('link', { name: /^Open / })).toHaveCount(1)
  await expect(page.getByRole('link', { name: 'Open Multan · Device 2, Health, offline' })).toBeVisible()
  await reportPlainWords(page, testInfo, '/devices')
  if (process.env.ADD_VISUAL_QA === '1') await page.screenshot({ path: testInfo.outputPath('devices-filtered.png'), fullPage: true })
})

test('device names and status chips are never cut, and the pages pass axe', async ({ page }) => {
  for (const route of ['/home', '/devices']) {
    await page.goto(route)
    await expect(page.locator('.app-header')).toBeVisible()
    await expect(page.getByRole('link', { name: /^Open Multan · Device 2/ }).first()).toBeVisible()
    const clipped = await page.evaluate(() => Array.from(document.querySelectorAll<HTMLElement>('.ui-chip, .device-name, .home-device-name'))
      .filter((element) => element.scrollWidth > element.clientWidth + 1 || getComputedStyle(element).textOverflow === 'ellipsis')
      .map((element) => element.textContent))
    expect(clipped).toEqual([])
    const width = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(width).toBeLessThanOrEqual(0)
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
    expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
  }
})
