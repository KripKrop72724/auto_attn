// Sweeps over every page (FND-1). Report-only for now: tap targets under
// 44 px, system words, motion left running with reduced motion, and the newer
// WCAG 2.2 AA rules are added to the test as annotations, not failures. Runs
// on the phone-390-touch and desktop-1440 projects only.
import AxeBuilder from '@axe-core/playwright'
import { DASHBOARD_ROUTES, expect, mockDashboard, test } from './fixtures'
import { reportPlainWords } from './plainWords'

test.beforeEach(async ({ page }) => mockDashboard(page))

test('every page: tap targets, plain words, reduced motion and WCAG 2.2 (report only)', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const touch = testInfo.project.name === 'phone-390-touch'
  for (const route of DASHBOARD_ROUTES) {
    await page.goto(route)
    await expect(page.locator('.app-header')).toBeVisible()
    await page.waitForTimeout(300)

    if (touch) {
      const small = await page.evaluate(() => Array.from(document.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea, [role="tab"], [role="button"]'))
        .filter((element) => {
          const box = element.getBoundingClientRect()
          const style = getComputedStyle(element)
          return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && (box.width < 44 || box.height < 44)
        })
        .slice(0, 20)
        .map((element) => `${element.tagName.toLowerCase()} "${(element.getAttribute('aria-label') || element.textContent || '').trim().slice(0, 40)}" ${Math.round(element.getBoundingClientRect().width)}×${Math.round(element.getBoundingClientRect().height)}`))
      for (const item of small) testInfo.annotations.push({ type: 'tap-target', description: `${route}: ${item}` })
    }

    await reportPlainWords(page, testInfo, route)

    const moving = await page.evaluate(() => document.getAnimations()
      .filter((animation) => {
        const timing = animation.effect?.getComputedTiming()
        const duration = typeof timing?.duration === 'number' ? timing.duration : 0
        return animation.playState === 'running' && (timing?.iterations === Infinity || duration > 1)
      })
      .map((animation) => (animation as CSSAnimation).animationName || 'animation'))
    for (const name of moving) testInfo.annotations.push({ type: 'reduced-motion', description: `${route}: ${name} still moving` })

    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze()
    for (const violation of results.violations.filter((item) => ['critical', 'serious'].includes(item.impact || ''))) {
      testInfo.annotations.push({ type: 'axe-wcag22', description: `${route}: ${violation.id} (${violation.nodes.length})` })
    }
  }
  // The sweep itself must reach every page.
  expect(DASHBOARD_ROUTES.length).toBeGreaterThan(9)
})
