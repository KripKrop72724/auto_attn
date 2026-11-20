// Recover attendance: recoveries, unreadable records and Not in HR system yet.
import AxeBuilder from '@axe-core/playwright'
import { expect, test, mockDashboard, sourceException } from './fixtures'

test.beforeEach(async ({ page }) => mockDashboard(page))

const records = (page: import('@playwright/test').Page) => page.getByRole('list', { name: 'Unreadable records' })

test('unreadable record sheet is responsive, keyboard-operable, and keeps its safeguard', async ({ page }) => {
  await page.goto('/recover')
  await expect(page.getByRole('heading', { name: 'Recover attendance', exact: true, level: 1 })).toBeVisible()
  // Phones show the short tab label "Unreadable" (plan: shorter tab names on phone).
  await page.getByRole('tab', { name: /^Unreadable( records)?\b/ }).click()
  await expect(page.getByRole('heading', { name: 'Unreadable records', level: 2 })).toBeVisible()
  const row = records(page).getByRole('listitem').filter({ hasText: 'Impossible time' })
  await expect(row).toBeVisible()
  // The raw code only lives inside Technical details.
  await expect(page.getByText('IMPLAUSIBLE_TERMINAL_TIME')).toBeHidden()
  await row.getByRole('button', { name: /^View record/ }).focus()
  await page.keyboard.press('Enter')
  const sheet = page.getByRole('dialog', { name: 'Unreadable record' })
  await expect(sheet).toBeVisible()
  await expect(sheet.getByText('Not sent to the HR system (Oracle)')).toBeVisible()
  await sheet.getByText('Technical details', { exact: true }).click()
  await sheet.getByLabel('Note (why you need the raw record)').fill('Inspect immutable source bytes for incident review.')
  await sheet.getByLabel('Your password to show it').fill('not-recorded-by-test')
  await sheet.getByRole('button', { name: 'Show raw record' }).click()
  await expect(sheet.getByText('ffffffffffffffff')).toBeVisible()
  await expect(sheet.getByText(/Hidden again in \d+ s\./)).toBeVisible()
  await sheet.getByRole('button', { name: 'Close', exact: true }).click()

  // Records that changed on the machine are reached from Recovery details › Technical details.
  await page.getByRole('tab', { name: /Recoveries/ }).click()
  await page.getByRole('button', { name: /More actions for/ }).first().click()
  await page.getByRole('menuitem', { name: 'Details' }).click()
  const details = page.getByRole('dialog', { name: 'Recovery details' })
  await details.getByText('Technical details', { exact: true }).last().click()
  await details.getByRole('button', { name: 'Record 5,130 changed on the machine' }).click()
  const changed = page.getByRole('dialog', { name: 'Record changed on the machine' })
  await expect(changed.getByText('Read again to confirm')).toBeVisible()
  await expect(changed.getByText(/Nothing already sent to the HR system is changed\./)).toBeVisible()
  await changed.getByText('Technical details', { exact: true }).click()
  await changed.getByLabel('Note (why you need the raw record)').fill('Inspect preserved terminal mutation evidence.')
  await changed.getByLabel('Your password to show it').fill('not-recorded-by-test')
  await changed.getByRole('button', { name: 'Show raw record' }).click()
  await expect(changed.getByText('ffffffffffffffff')).toBeVisible()
  const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
})

test('records not matched to an employee stay explicit and readable after they are seen', async ({ page }, testInfo) => {
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
      totals: { all: 1, open: 1, reviewed: 0, invalid_time: 0, malformed: 0,
        identity_unresolved: 1, affected_terminals: 1 },
      rows: [held], next_cursor: null, filtered_total: 1,
    } })
  })
  // The old address still lands on the tab.
  await page.goto('/reconciliation?tab=source-exceptions')
  await expect(page).toHaveURL(/\/recover\?tab=unreadable/)
  await expect(page.getByRole('tab', { name: /^Unreadable( records)? 1$/ })).toBeVisible()
  await expect(records(page).getByText('Not sent: not matched to an employee')).toBeVisible()
  const viewport = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }))
  expect(viewport.scroll).toBeLessThanOrEqual(viewport.width)
  if (process.env.ADD_VISUAL_QA === '1') await page.screenshot({ path: testInfo.outputPath('source-identity-ledger.png'), fullPage: true, animations: 'disabled' })
  await records(page).getByRole('listitem').getByRole('button', { name: /^View record/ }).click()
  const sheet = page.getByRole('dialog', { name: 'Unreadable record' })
  await expect(sheet.getByRole('heading', { name: 'Not matched to an employee' })).toBeVisible()
  await expect(sheet.getByText(/Marking it as seen will not send it/)).toBeVisible()
  await sheet.evaluate(async node => {
    await Promise.all(node.getAnimations().map(animation => animation.finished.catch(() => undefined)))
  })
  if (process.env.ADD_VISUAL_QA === '1') await sheet.screenshot({ path: testInfo.outputPath('source-identity-detail.png'), animations: 'disabled' })
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations.filter(violation => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
})

test('a long recovery history stays viewport-bound and shows 25 more at a time', async ({ page }) => {
  await page.goto('/recover')
  const more = page.getByRole('button', { name: /More actions for/ }).first()
  await more.click()
  await page.getByRole('menuitem', { name: 'Details' }).click()
  const sheet = page.getByRole('dialog', { name: 'Recovery details' })
  await expect(sheet).toBeVisible()
  const events = sheet.getByRole('list', { name: 'What happened' }).getByRole('listitem')
  await expect(events).toHaveCount(25)
  await expect(sheet.getByText('Newest first')).toBeVisible()
  await expect(sheet.getByText('Showing 25 of 899')).toBeVisible()

  const geometry = await page.evaluate(() => {
    const viewport = window.visualViewport
    const backdrop = document.querySelector<HTMLElement>('.ui-sheet-backdrop')?.getBoundingClientRect()
    const panel = document.querySelector<HTMLElement>('.recover-details-sheet')?.getBoundingClientRect()
    return {
      width: viewport?.width || window.innerWidth,
      height: viewport?.height || window.innerHeight,
      backdrop: backdrop && { x: backdrop.x, y: backdrop.y, right: backdrop.right, bottom: backdrop.bottom },
      panel: panel && { x: panel.x, y: panel.y, right: panel.right, bottom: panel.bottom },
      rootInert: document.getElementById('root')?.hasAttribute('inert'),
    }
  })
  expect(geometry.backdrop?.x).toBeGreaterThanOrEqual(-1)
  expect(geometry.backdrop?.y).toBeGreaterThanOrEqual(-1)
  expect(geometry.backdrop?.right).toBeLessThanOrEqual(geometry.width + 1)
  expect(geometry.backdrop?.bottom).toBeLessThanOrEqual(geometry.height + 1)
  expect(geometry.panel?.x).toBeGreaterThanOrEqual(-1)
  expect(geometry.panel?.y).toBeGreaterThanOrEqual(-1)
  expect(geometry.panel?.right).toBeLessThanOrEqual(geometry.width + 1)
  expect(geometry.panel?.bottom).toBeLessThanOrEqual(geometry.height + 1)
  expect(geometry.rootInert).toBe(true)

  await sheet.getByRole('button', { name: 'Show 25 more' }).click()
  await expect(events).toHaveCount(50)
  await sheet.getByRole('button', { name: 'Close', exact: true }).click()
  await expect(more).toBeFocused()
  await expect(page.locator('#root')).not.toHaveAttribute('inert', '')
})

test('each tab reads in plain words and fits the screen', async ({ page }) => {
  for (const tab of ['', '?tab=unreadable', '?tab=held']) {
    await page.goto(`/recover${tab}`)
    await expect(page.getByRole('heading', { name: 'Recover attendance', level: 1 })).toBeVisible()
    await expect(page.getByRole('tabpanel')).toBeVisible()
    const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
    expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
    await expect(page.getByRole('tabpanel').getByText(/Reconciliation|Oracle assurance|Source exceptions|SAFE DELIVERY LANE/)).toHaveCount(0)
  }
  await expect(page.getByText('Not in HR system yet', { exact: true }).first()).toBeVisible()
  await expect(page.getByText(/Preview filtered rows|Approve and queue batch/)).toHaveCount(0)
})
