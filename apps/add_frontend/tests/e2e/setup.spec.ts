// Set up a device.
import AxeBuilder from '@axe-core/playwright'
import { expect, test, expectOnlyAllowedStorage, installerApps, mockDashboard } from './fixtures'

test.beforeEach(async ({ page }) => mockDashboard(page))

test('physical provisioning environment is responsive, explicit, and accessible', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'userAgent', {
      configurable: true,
      get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36',
    })
  })
  // The old Prepare link redirects to Set up a device.
  await page.goto('/firmware?tab=prepare')
  await expect(page).toHaveURL(/\/setup$/)
  await expect(page.getByRole('heading', { name: 'Set up a device', exact: true, level: 1 })).toBeVisible()
  // The fixture's server can start the browser setup, so the older wizard follows the installer card.
  await expect(page.getByRole('heading', { name: 'Connect the provisioning companion' })).toBeVisible()
  await expect(page.getByText('c'.repeat(64))).toBeVisible()
  // The State Life Device Installer card leads the page: Windows first on Windows.
  const installer = page.getByRole('region', { name: 'State Life Device Installer' })
  await expect(installer.getByRole('link', { name: 'Download for Windows' })).toHaveAttribute('href', installerApps.latest.files[0].download_path)
  await expect(installer.getByRole('link', { name: 'Download for Mac' })).toHaveAttribute('href', installerApps.latest.files[1].download_path)
  await expect(installer.getByText(/Windows protected your PC/)).toBeVisible()
  await expect(installer.getByText("Copies older than version 1.0.0 can't set up link boxes.")).toBeVisible()
  // The SHA-256 sits under Technical details: open it first.
  await expect(installer.getByText('d'.repeat(64))).toBeHidden()
  await installer.locator('summary', { hasText: 'Technical details (Windows file)' }).click()
  await expect(installer.getByText('d'.repeat(64))).toBeVisible()
  await expect(page.getByText('APP FOR SITE STAFF')).toHaveCount(0)
  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    content: document.documentElement.scrollWidth,
    offenders: Array.from(document.querySelectorAll<HTMLElement>('body *'))
      .filter((element) => element.getBoundingClientRect().right > window.innerWidth + 1)
      .slice(0, 8)
      .map((element) => ({ tag: element.tagName, className: element.className, parent: element.parentElement?.className, right: Math.round(element.getBoundingClientRect().right), scrollWidth: element.scrollWidth })),
  }))
  expect(dimensions.content, JSON.stringify(dimensions.offenders)).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
  await expectOnlyAllowedStorage(page)
})

test('setting up is one plain switched-off card while the installer is off', async ({ page }) => {
  await page.route('**/api/v1/installer/apps', (route) => route.fulfill({
    status: 409, contentType: 'application/json',
    body: JSON.stringify({ code: 'INSTALLER_DISABLED', message: 'Off.', detail: { code: 'INSTALLER_DISABLED', message: 'Off.' } }),
  }))
  await page.goto('/setup')
  await expect(page.getByRole('heading', { name: 'Set up a device', exact: true, level: 1 })).toBeVisible()
  await expect(page.getByText('Switched off for now')).toBeVisible()
  await expect(page.getByText('Need a new device? Ask IT.')).toBeVisible()
  await expect(page.getByText(/Physical provisioning is disabled/)).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'State Life Device Installer' })).toHaveCount(0)
  const width = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(width.content).toBeLessThanOrEqual(width.viewport)
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
})
