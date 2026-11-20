// WORDS-11: the plain-words guard is strict on every page in STRICT_ROUTES at
// phone and desktop width. Anything a person can read outside Technical
// details (and the other marked parts) must be plain words. The same check
// runs in jsdom for every route in src/App.plainWords.test.tsx.
import { DASHBOARD_ROUTES, expect, mockDashboard, test } from './fixtures'
import { reportPlainWords, STRICT_ROUTES } from './plainWords'

test.beforeEach(async ({ page }) => mockDashboard(page))

test('plain words on every page', async ({ page }, testInfo) => {
  test.skip(!['phone-390-touch', 'desktop-1440'].includes(testInfo.project.name), 'Runs at 390 and 1440 px only.')
  test.setTimeout(120_000)
  const failures: string[] = []
  for (const route of DASHBOARD_ROUTES) {
    await page.goto(route)
    await expect(page.locator('.app-header')).toBeVisible()
    await page.waitForTimeout(400)
    const problems = await reportPlainWords(page, testInfo, route)
    if (STRICT_ROUTES.includes(route)) failures.push(...problems.map((problem) => `${route}: ${problem.rule} "${problem.text}"`))
  }
  expect(failures).toEqual([])
})
