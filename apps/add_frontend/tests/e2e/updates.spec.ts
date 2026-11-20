// Software updates (08-advanced 2, ADV-23): each tab fits the screen, passes
// axe and keeps only the allowed browser storage. Old Firmware links redirect.
import AxeBuilder from '@axe-core/playwright'
import { expect, test, expectOnlyAllowedStorage, mockDashboard } from './fixtures'

test.beforeEach(async ({ page }) => mockDashboard(page))

for (const [tab, heading] of [['overview', 'Newest versions'], ['updates', 'Updates'], ['versions', 'Software versions']] as const) {
  test(`Software updates ${tab} tab fits the screen and passes axe`, async ({ page }, testInfo) => {
    await page.goto(`/updates?tab=${tab}`)
    await expect(page.getByRole('heading', { name: 'Software updates', exact: true, level: 1 })).toBeVisible()
    await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible()
    await expect(page.getByRole('tab', { name: /Overview/ })).toBeVisible()
    await expect(page.getByRole('tab', { name: /Versions/ })).toBeVisible()
    const width = await page.evaluate(() => ({
      viewport: window.innerWidth,
      content: document.documentElement.scrollWidth,
      offenders: Array.from(document.querySelectorAll<HTMLElement>('body *'))
        .filter((element) => element.getBoundingClientRect().right > window.innerWidth + 1)
        .slice(0, 8).map((element) => element.className),
    }))
    expect(width.content, JSON.stringify(width.offenders)).toBeLessThanOrEqual(width.viewport)
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
    expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
    if (process.env.ADD_VISUAL_QA === '1') await page.screenshot({ path: testInfo.outputPath(`updates-${tab}.png`), fullPage: true })
    await expectOnlyAllowedStorage(page)
  })
}

test('old Firmware links open the matching Software updates tab', async ({ page }) => {
  await page.goto('/firmware?tab=releases')
  await expect(page).toHaveURL(/\/updates\?tab=versions$/)
  await expect(page.getByRole('heading', { name: 'Software versions' })).toBeVisible()
  await page.goto('/firmware?tab=campaigns')
  await expect(page).toHaveURL(/\/updates\?tab=updates$/)
})

test('the Update software sheet opens, lists devices by division and closes back to its button', async ({ page }) => {
  await page.goto('/updates')
  const open = page.getByRole('button', { name: 'Update software' })
  await expect(open).toBeEnabled()
  await open.click()
  const sheet = page.getByRole('dialog', { name: 'Update software' })
  await expect(sheet).toBeVisible()
  await expect(sheet.getByRole('radio', { name: /2\.2\.30/ })).toBeChecked()
  await expect(sheet.getByLabel(/password/i)).toHaveCount(0)
  await sheet.getByRole('button', { name: 'Close' }).first().click()
  await expect(sheet).toHaveCount(0)
  await expect(open).toBeFocused()
})
