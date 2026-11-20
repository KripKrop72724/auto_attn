// Set up a device.
import AxeBuilder from '@axe-core/playwright'
import { expect, test, device, expectOnlyAllowedStorage, installerApps, mockDashboard } from './fixtures'

test.beforeEach(async ({ page }) => mockDashboard(page))

test('physical provisioning environment is responsive, explicit, and accessible', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'userAgent', {
      configurable: true,
      get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36',
    })
  })
  await page.goto('/setup')
  await expect(page.getByRole('heading', { name: 'Firmware', exact: true, level: 1 })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Prepare device' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('heading', { name: 'Connect the provisioning companion' })).toBeVisible()
  await expect(page.getByText('c'.repeat(64))).toBeVisible()
  await expect(page.getByText(/not OS code-signed/i)).toBeVisible()
  // The State Life Device Installer card leads the page: Windows first on Windows.
  const installer = page.getByRole('region', { name: 'State Life Device Installer' })
  await expect(installer.getByRole('link', { name: 'Download for Windows' })).toHaveAttribute('href', installerApps.latest.files[0].download_path)
  await expect(installer.getByRole('link', { name: 'Download for Mac' })).toHaveAttribute('href', installerApps.latest.files[1].download_path)
  await expect(installer.getByText('d'.repeat(64))).toBeVisible()
  await expect(installer.getByText(/Windows protected your PC/)).toBeVisible()
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
