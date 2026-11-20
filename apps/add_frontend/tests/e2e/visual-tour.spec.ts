// Full-page screenshots of every page for the PR review (FND-1, DLV-3). Runs
// when ADD_VISUAL_QA=1 (CI sets it) on phone-390-touch and desktop-1440; the
// PNGs land in apps/add_frontend/screenshots/{page}-{project}.png and CI
// uploads them as the add-ui-screenshots artifact.
import { DASHBOARD_ROUTES, expect, mockDashboard, test } from './fixtures'

const fileName = (route: string) => route.replace(/^\//, '').replace(/[^a-z0-9]+/gi, '-') || 'root'

test.beforeEach(async ({ page }) => mockDashboard(page))

test('screenshot tour of every page', async ({ page }, testInfo) => {
  test.skip(process.env.ADD_VISUAL_QA !== '1', 'Screenshots are taken in CI (ADD_VISUAL_QA=1).')
  test.setTimeout(180_000)
  const shot = (name: string) => `screenshots/${name}-${testInfo.project.name}.png`
  for (const route of DASHBOARD_ROUTES) {
    await page.goto(route)
    await expect(page.locator('.app-header')).toBeVisible()
    await page.waitForTimeout(400)
    await page.screenshot({ path: shot(fileName(route)), fullPage: true })
  }
  if ((page.viewportSize()?.width || 0) <= 760) {
    await page.goto('/home')
    await page.getByRole('button', { name: 'More', exact: true }).click()
    await page.waitForTimeout(300)
    await page.screenshot({ path: shot('more-sheet'), fullPage: true })
  }
})

test('screenshot of the sign-in page', async ({ page }, testInfo) => {
  test.skip(process.env.ADD_VISUAL_QA !== '1', 'Screenshots are taken in CI (ADD_VISUAL_QA=1).')
  await page.route('**/api/v1/auth/session', (route) => route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"Not signed in"}' }))
  await page.goto('/home')
  await page.waitForTimeout(500)
  await page.screenshot({ path: `screenshots/sign-in-${testInfo.project.name}.png`, fullPage: true })
})
