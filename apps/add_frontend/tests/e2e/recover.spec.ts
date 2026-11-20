// Recover attendance: recoveries and unreadable records.
import AxeBuilder from '@axe-core/playwright'
import { expect, test, mockDashboard, sourceException } from './fixtures'

test.beforeEach(async ({ page }) => mockDashboard(page))

test('source exception inspector is responsive, keyboard-operable, and fail-closed', async ({ page }) => {
  await page.goto('/recover')
  await expect(page.getByRole('heading', { name: 'Reconciliation', exact: true, level: 1 })).toBeVisible()
  await page.getByRole('tab', { name: /Source exceptions/ }).click()
  await expect(page.getByRole('heading', { name: 'Immutable source exception ledger' })).toBeVisible()
  await expect(page.getByText('IMPLAUSIBLE_TERMINAL_TIME')).toBeVisible()
  const exceptionRow = page.getByRole('listitem').filter({ hasText: 'IMPLAUSIBLE_TERMINAL_TIME' })
  await exceptionRow.getByRole('button', { name: 'Inspect' }).focus()
  await page.keyboard.press('Enter')
  const drawer = page.getByRole('dialog', { name: 'Terminal source exception' })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByText('Excluded from attendance and Oracle')).toBeVisible()
  await drawer.getByLabel('Audited reason').fill('Inspect immutable source bytes for incident review.')
  await drawer.getByLabel('Administrator password').fill('not-recorded-by-test')
  await drawer.getByRole('button', { name: 'Reveal protected bytes' }).click()
  await expect(drawer.getByText('ffffffffffffffff')).toBeVisible()
  await drawer.getByRole('button', { name: 'Close', exact: true }).click()
  await page.getByRole('button', { name: /SLICTOWER · 3rd Floor · ordinal 5,130/ }).click()
  const divergenceDrawer = page.getByRole('dialog', { name: 'Terminal-history change' })
  await expect(divergenceDrawer.getByText('Independent fresh-buffer verification')).toBeVisible()
  await expect(divergenceDrawer.getByText(/Old evidence remains immutable/)).toBeVisible()
  await divergenceDrawer.getByLabel('Audited reason').fill('Inspect preserved terminal mutation evidence.')
  await divergenceDrawer.getByLabel('Administrator password').fill('not-recorded-by-test')
  await divergenceDrawer.getByRole('button', { name: 'Reveal protected bytes' }).click()
  await expect(divergenceDrawer.getByText('ffffffffffffffff')).toBeVisible()
  const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
})

test('historical source identity holds remain explicit and readable after review', async ({ page }, testInfo) => {
  const held = { ...sourceException, disposition: 'IDENTITY_UNRESOLVED',
    error_code: 'HISTORICAL_IDENTITY_EVIDENCE_REQUIRED', observed_uid: '40',
    observed_user_id: null, raw_timestamp: 859972462, review_state: 'REVIEWED',
    reviewed_at: '2026-10-03T10:10:00Z', reviewed_by: 'StateHealthAdmin',
    reviews: [{ review_id: 'source-identity-note', state: 'REVIEWED',
      reason: 'Missing historical reference remains under investigation.',
      actor: 'StateHealthAdmin', created_at: '2026-10-03T10:10:00Z' }],
    oracle_action: 'HELD_IDENTITY_EVIDENCE_REQUIRED' }
  await page.route(url => url.pathname.startsWith('/api/v1/source-exceptions'), async route => {
    const path = new URL(route.request().url()).pathname
    await route.fulfill({ json: path.endsWith('/1') ? held : {
      totals: { all: 1, open: 0, reviewed: 1, invalid_time: 0, malformed: 0,
        identity_unresolved: 1, affected_terminals: 1 },
      rows: [held], next_cursor: null, filtered_total: 1,
    } })
  })
  await page.goto('/recover?tab=unreadable')
  await expect(page.getByText('Missing identity references', { exact: true })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Source exceptions 1', exact: true })).toBeVisible()
  await expect(page.getByText('Oracle delivery held for identity evidence')).toBeVisible()
  // Jobs load independently of source rows. Its history panel changes the
  // layout; wait for that known fixture before measuring or clicking below it.
  await expect(page.getByRole('heading', { name: 'Preserved terminal-history changes' })).toBeVisible()
  const viewport = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }))
  expect(viewport.scroll).toBeLessThanOrEqual(viewport.width)
  if (process.env.ADD_VISUAL_QA === '1') await page.screenshot({ path: testInfo.outputPath('source-identity-ledger.png'), fullPage: true, animations: 'disabled' })
  await page.getByRole('listitem').getByRole('button', { name: 'Inspect' }).click()
  const drawer = page.getByRole('dialog', { name: 'Terminal source exception' })
  await expect(drawer.getByText('Held for verified historical identity')).toBeVisible()
  await expect(drawer.getByText(/A review note does not resolve this hold/)).toBeVisible()
  await drawer.evaluate(async node => {
    await Promise.all(node.getAnimations().map(animation => animation.finished.catch(() => undefined)))
  })
  if (process.env.ADD_VISUAL_QA === '1') await drawer.screenshot({ path: testInfo.outputPath('source-identity-detail.png'), animations: 'disabled' })
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter(violation => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
})

test('large reconciliation evidence stays viewport-bound and progressively reveals events', async ({ page }) => {
  await page.goto('/recover')
  const inspect = page.getByRole('button', { name: 'Inspect evidence' }).first()
  await inspect.click()
  const drawer = page.getByRole('dialog', { name: 'Reconciliation evidence' })
  await expect(drawer).toBeVisible()
  await expect(drawer.locator('.reconciliation-event-summary')).toHaveCount(25)
  await expect(drawer.getByText('Showing 25 newest')).toBeVisible()

  const longStatusGeometry = await drawer.getByLabel('Status: SOURCE CAPTURE CERTIFIED WITH IMMUTABLE CHAIN EVIDENCE').first().evaluate((badge) => {
    const box = badge.getBoundingClientRect()
    const copy = badge.parentElement?.nextElementSibling?.getBoundingClientRect()
    const text = badge.querySelector('span')
    return {
      display: getComputedStyle(badge).display,
      flexDirection: getComputedStyle(badge).flexDirection,
      whiteSpace: text ? getComputedStyle(text).whiteSpace : '',
      nonOverlapping: Boolean(copy && (box.right <= copy.left || copy.right <= box.left || box.bottom <= copy.top || copy.bottom <= box.top)),
      box: { left: box.left, top: box.top, right: box.right, bottom: box.bottom },
      copy: copy ? { left: copy.left, top: copy.top, right: copy.right, bottom: copy.bottom } : null,
      title: badge.getAttribute('title'),
    }
  })
  expect(['flex', 'inline-flex']).toContain(longStatusGeometry.display)
  expect(longStatusGeometry.flexDirection).toBe('row')
  expect(longStatusGeometry.whiteSpace).toBe('nowrap')
  expect(longStatusGeometry.nonOverlapping, JSON.stringify(longStatusGeometry)).toBe(true)
  expect(longStatusGeometry.title).toBe('SOURCE CAPTURE CERTIFIED WITH IMMUTABLE CHAIN EVIDENCE')

  const geometry = await page.evaluate(() => {
    const viewport = window.visualViewport
    const backdrop = document.querySelector<HTMLElement>('.dialog-backdrop')?.getBoundingClientRect()
    const dialog = document.querySelector<HTMLElement>('.reconciliation-detail-drawer')?.getBoundingClientRect()
    const workspace = document.querySelector<HTMLElement>('.app-workspace')
    return {
      width: viewport?.width || window.innerWidth,
      height: viewport?.height || window.innerHeight,
      backdrop: backdrop && { x: backdrop.x, y: backdrop.y, right: backdrop.right, bottom: backdrop.bottom },
      dialog: dialog && { x: dialog.x, y: dialog.y, right: dialog.right, bottom: dialog.bottom },
      workspaceOverflow: workspace?.style.overflow,
      rootInert: document.getElementById('root')?.hasAttribute('inert'),
    }
  })
  expect(geometry.backdrop?.x).toBeGreaterThanOrEqual(-1)
  expect(geometry.backdrop?.y).toBeGreaterThanOrEqual(-1)
  expect(geometry.backdrop?.right).toBeLessThanOrEqual(geometry.width + 1)
  expect(geometry.backdrop?.bottom).toBeLessThanOrEqual(geometry.height + 1)
  expect(geometry.dialog?.x).toBeGreaterThanOrEqual(-1)
  expect(geometry.dialog?.y).toBeGreaterThanOrEqual(-1)
  expect(geometry.dialog?.right).toBeLessThanOrEqual(geometry.width + 1)
  expect(geometry.dialog?.bottom).toBeLessThanOrEqual(geometry.height + 1)
  expect(geometry.workspaceOverflow).toBe('hidden')
  expect(geometry.rootInert).toBe(true)

  await drawer.locator('.reconciliation-event-summary').first().click()
  await expect(drawer.locator('.reconciliation-event-ledger article pre')).toHaveCount(1)
  const expandedBox = await drawer.locator('.reconciliation-event-ledger article pre').boundingBox()
  const drawerBox = await drawer.boundingBox()
  expect((expandedBox?.x || 0) + (expandedBox?.width || 0)).toBeLessThanOrEqual((drawerBox?.x || 0) + (drawerBox?.width || 0) + 1)

  await drawer.getByRole('button', { name: 'Load 25 older events' }).click()
  await expect(drawer.locator('.reconciliation-event-summary')).toHaveCount(50)
  await drawer.getByRole('button', { name: 'Close', exact: true }).click()
  await expect(inspect).toBeFocused()
  await expect(page.locator('#root')).not.toHaveAttribute('inert', '')
})
