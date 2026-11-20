// The app frame: live updates, menu, routes, layout at every size.
import AxeBuilder from '@axe-core/playwright'
import { expect, test, device, expectOnlyAllowedStorage, mockDashboard, nationwideDevices, representativeUser } from './fixtures'
import { reportPlainWords } from './plainWords'

test.beforeEach(async ({ page }) => mockDashboard(page))

test('a permanently closed event stream reconnects while fleet snapshots keep refreshing', async ({ page }) => {
  let streamRequests = 0
  let fleetRequests = 0
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/v1/devices') fleetRequests += 1
  })
  await page.route('**/events/v1/stream', async route => {
    streamRequests += 1
    // HTTP 204 puts the browser's real EventSource in CLOSED; its native
    // transient-error retry cannot recover this connection.
    await route.fulfill({ status: 204 })
  })
  await page.clock.install({ time: new Date('2026-10-04T09:00:00Z') })
  // Freeze before app startup so real network scheduling cannot move an
  // attempt a few milliseconds past the next 30-second clock boundary.
  await page.clock.pauseAt(new Date('2026-10-04T09:00:01Z'))
  await page.goto('/home')
  await expect(page.getByRole('heading', { name: 'Home', exact: true, level: 1 })).toBeVisible()
  await expect.poll(() => streamRequests).toBe(1)
  await expect(page.locator('.live-sync')).toHaveText('Reconnecting…')
  const initialFleetRequests = fleetRequests
  await page.clock.runFor(30_200)
  await expect.poll(() => streamRequests).toBe(2)
  await expect.poll(() => fleetRequests).toBeGreaterThan(initialFleetRequests)
  const phone = (page.viewportSize()?.width || 0) <= 760
  await expect(page.locator('.live-sync')).toHaveText(phone ? 'Offline' : 'Offline · showing last saved data')
  await expect(page.locator('.live-banner')).toContainText('Trying again…')
  await expect(page.getByText('Cached data', { exact: true })).toHaveCount(0)
  await expect(page.getByText('Live transport', { exact: true })).toHaveCount(0)
  await page.clock.runFor(30_000)
  await expect.poll(() => streamRequests).toBe(3)
})

test('adaptive shell has no horizontal overflow and meets critical accessibility checks', async ({ page }) => {
  const check = async () => {
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
  }
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/home')
  await expect(page.getByRole('heading', { name: 'Home', exact: true, level: 1 })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Summary' }).getByRole('link')).toHaveCount(4)
  await expect(page.getByRole('region', { name: 'Life' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Health' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Pakistan device network map' })).toHaveCount(0)
  await check()
  await page.goto('/devices')
  await expect(page.getByRole('link', { name: 'Open SLIC Tower · 3rd Floor, Health, working' })).toBeVisible()
  await check()
})

test('primary routes and device deep link remain usable', async ({ page }, testInfo) => {
  await page.goto('/devices')
  await page.getByRole('link', { name: 'Open SLIC Tower · 3rd Floor, Health, working' }).click()
  await expect(page).toHaveURL(/\/devices\/connector-one$/)
  await expect(page.getByRole('dialog', { name: 'SLICTOWER · 3rd Floor' })).toBeVisible()
  const custody = page.getByRole('article', { name: 'ZKT ADD custody' })
  await expect(custody.getByRole('heading', { name: 'Preserved records need processing repair' })).toBeVisible()
  await custody.locator('summary').click()
  await expect(custody.getByText('Protocol review', { exact: true })).toBeVisible()
  await expect(custody.getByText('Not established by custody')).toBeVisible()
  await expect(custody.getByText('Proposed facts · qualification required')).toBeVisible()
  await expect(custody.getByRole('heading', { name: 'ADD worker · all ZKT connectors' })).toBeVisible()
  await expect(custody.getByText('Stalled', { exact: true })).toBeVisible()
  const bounds = await custody.evaluate(element => ({ width: element.clientWidth, scroll: element.scrollWidth }))
  expect(bounds.scroll).toBeLessThanOrEqual(bounds.width + 1)
  if (process.env.ADD_VISUAL_QA === '1') await custody.screenshot({ path: testInfo.outputPath('custody-panel.png') })
  await page.getByRole('button', { name: 'Close', exact: true }).click()

  const primaryNav = page.getByRole('navigation', { name: 'Main menu' })
  const phone = (page.viewportSize()?.width || 0) <= 760
  // On a phone People and the Advanced tools live in the More sheet.
  const openFromMore = async (name: RegExp) => {
    await primaryNav.getByRole('button', { name: 'More', exact: true }).click()
    await page.getByRole('dialog', { name: 'More' }).getByRole('link', { name }).click()
  }
  if (phone) await openFromMore(/^People/)
  else await primaryNav.getByRole('link', { name: 'People', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Users', exact: true, level: 1 })).toBeVisible()
  await primaryNav.getByRole('link', { name: 'Attendance', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Live attendance' })).toBeVisible()
  if (phone) {
    const moreTrigger = primaryNav.getByRole('button', { name: 'More', exact: true })
    await moreTrigger.click()
    await expect(page.getByRole('dialog', { name: 'More' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog', { name: 'More' })).toHaveCount(0)
    await expect(moreTrigger).toBeFocused()
    await openFromMore(/^Recover attendance/)
    await expect(page.getByRole('heading', { name: 'Reconciliation', exact: true, level: 1 })).toBeVisible()
    await openFromMore(/^Software updates/)
  } else {
    await primaryNav.getByRole('link', { name: 'Recover attendance', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Reconciliation', exact: true, level: 1 })).toBeVisible()
    await primaryNav.getByRole('link', { name: 'Software updates', exact: true }).click()
  }
  await expect(page.getByRole('heading', { name: 'Firmware', exact: true, level: 1 })).toBeVisible()
  const channelBadgeGeometry = await page.locator('.firmware-channel-strip .status-badge').first().evaluate((badge) => {
    const icon = badge.querySelector('svg')?.getBoundingClientRect()
    const text = badge.querySelector('span')?.getBoundingClientRect()
    const box = badge.getBoundingClientRect()
    return {
      display: getComputedStyle(badge).display,
      flexDirection: getComputedStyle(badge).flexDirection,
      height: box.height,
      horizontal: Boolean(icon && text && icon.right <= text.left && Math.abs((icon.top + icon.height / 2) - (text.top + text.height / 2)) <= 2),
    }
  })
  expect(['flex', 'inline-flex']).toContain(channelBadgeGeometry.display)
  expect(channelBadgeGeometry.flexDirection).toBe('row')
  expect(channelBadgeGeometry.height).toBeLessThanOrEqual(32)
  expect(channelBadgeGeometry.horizontal).toBe(true)
  await page.getByRole('tab', { name: /Signed releases/ }).click()
  await expect(page.getByRole('heading', { name: 'Signed release inventory' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Zone Lite 2.2.30' })).toBeVisible()
  await page.getByRole('tab', { name: /Campaigns/ }).click()
  await expect(page.getByRole('heading', { name: 'Campaign operations' })).toBeVisible()
  await primaryNav.getByRole('link', { name: /^Problems/ }).click()
  await expect(page.getByRole('heading', { name: 'Alerts', exact: true, level: 1 })).toBeVisible()
  expect(page.url()).not.toMatch(/cnic|password|reason|confirmation/i)
  await expectOnlyAllowedStorage(page)
})

test('reported viewport thresholds, route scroll reset, and collision layers remain bounded', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-1280', 'Threshold matrix runs once in Chromium; Firefox and WebKit retain their dedicated projects.')
  const sizes = [
    { width: 320, height: 720 }, { width: 390, height: 844 },
    { width: 759, height: 800 }, { width: 760, height: 800 }, { width: 761, height: 800 },
    { width: 768, height: 1024 }, { width: 1024, height: 600 }, { width: 1224, height: 697 },
    { width: 1279, height: 720 }, { width: 1280, height: 720 }, { width: 1281, height: 720 },
    { width: 1440, height: 900 }, { width: 1600, height: 1000 },
  ]
  const routes = ['/home', '/people/connector-one', '/attendance', '/recover', '/updates', '/setup', '/problems', '/devices', '/settings']
  for (let index = 0; index < sizes.length; index += 1) {
    await page.setViewportSize(sizes[index])
    await page.goto(routes[index % routes.length])
    await expect(page.locator('.app-header')).toBeVisible()
    await page.waitForTimeout(200)
    const bounds = await page.evaluate(() => ({
      width: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      shell: document.querySelector<HTMLElement>('.app-shell')?.getBoundingClientRect().height || 0,
      workspace: document.querySelector<HTMLElement>('.app-workspace')?.getBoundingClientRect().height || 0,
      viewportHeight: window.innerHeight,
      offenders: Array.from(document.querySelectorAll<HTMLElement>('body *')).filter((element) => {
        const rect = element.getBoundingClientRect()
        return rect.right > window.innerWidth + 1 || rect.left < -1
      }).slice(0, 8).map((element) => ({ tag: element.tagName, text: element.textContent?.trim().slice(0, 30), className: element.className, parent: element.parentElement?.className, right: Math.round(element.getBoundingClientRect().right), scrollWidth: element.scrollWidth })),
    }))
    expect(bounds.documentWidth, `${sizes[index].width}x${sizes[index].height} on ${routes[index % routes.length]} offenders=${JSON.stringify(bounds.offenders)}`).toBeLessThanOrEqual(bounds.width)
    expect(Math.abs(bounds.shell - bounds.viewportHeight)).toBeLessThanOrEqual(1)
    expect(Math.abs(bounds.workspace - bounds.viewportHeight)).toBeLessThanOrEqual(1)
  }

  await page.setViewportSize({ width: 1224, height: 697 })
  await page.goto('/people/connector-one')
  const workspace = page.locator('.app-workspace')
  await expect(page.getByRole('article', { name: /Ayesha Khan, user 1007/i })).toBeVisible()
  await page.waitForTimeout(120)
  await workspace.evaluate((node) => { node.scrollTop = Math.min(360, node.scrollHeight - node.clientHeight) })
  const savedScroll = await workspace.evaluate((node) => node.scrollTop)
  expect(savedScroll).toBeGreaterThan(0)
  await page.getByRole('navigation', { name: 'Main menu' }).getByRole('link', { name: 'Software updates', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Firmware', exact: true, level: 1 })).toBeVisible()
  await expect.poll(() => workspace.evaluate((node) => node.scrollTop)).toBe(0)
  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Users', exact: true, level: 1 })).toBeVisible()
  await expect.poll(() => workspace.evaluate((node) => node.scrollTop)).toBe(savedScroll)

  await page.route('**/api/v1/devices*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: nationwideDevices }) }))
  await page.goto('/people')
  const pickerLayer = page.locator('.terminal-picker-layer')
  await expect(pickerLayer).toBeVisible()
  const finalOption = pickerLayer.getByRole('option').last()
  await finalOption.scrollIntoViewIfNeeded()
  const layerBox = await pickerLayer.boundingBox()
  const finalOptionBox = await finalOption.boundingBox()
  expect(layerBox?.x || 0).toBeGreaterThanOrEqual(11)
  expect((layerBox?.x || 0) + (layerBox?.width || 0)).toBeLessThanOrEqual(1224 - 11)
  expect(layerBox?.y || 0).toBeGreaterThanOrEqual(11)
  expect((layerBox?.y || 0) + (layerBox?.height || 0)).toBeLessThanOrEqual(697 - 11)
  expect(finalOptionBox?.y || 0).toBeGreaterThanOrEqual((layerBox?.y || 0) - 1)
  expect((finalOptionBox?.y || 0) + (finalOptionBox?.height || 0)).toBeLessThanOrEqual((layerBox?.y || 0) + (layerBox?.height || 0) + 1)
  await finalOption.click()
  await expect(page).toHaveURL(/\/people\/tower-thirteen$/)
  await expect(page.getByRole('button', { name: 'Change terminal' })).toBeVisible()

  const directoryUsers = Array.from({ length: 43 }, (_, index) => ({ ...representativeUser, id: index + 1, user_key: `operator-${index + 1}`, uid: `${index + 1}`, user_id: `${40_000 + index}`, display_name: `Operator ${index + 1}` }))
  await page.route('**/api/v2/devices/*/users*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: directoryUsers, next_cursor: null, device: nationwideDevices[4], identity_integrity: { source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT', total_users: 43, with_cnic: 43, missing_cnic: 0, duplicate_groups: 0, duplicate_users: 0, resolved_duplicate_groups: 0, unresolved_duplicate_groups: 0, unresolved_duplicate_users: 0 } }) }))
  await page.goto('/people/tower-three')
  const finalRow = page.getByRole('article', { name: /Operator 43, user 40042/i })
  await finalRow.scrollIntoViewIfNeeded()
  const finalMore = finalRow.getByLabel('More actions for Operator 43')
  await finalMore.click()
  const actionLayer = page.locator('.user-action-layer')
  await expect(actionLayer.getByRole('group', { name: 'Actions for Operator 43' })).toBeVisible()
  const actionBox = await actionLayer.boundingBox()
  expect(actionBox?.y || 0).toBeGreaterThanOrEqual(11)
  expect((actionBox?.y || 0) + (actionBox?.height || 0)).toBeLessThanOrEqual(697 - 11)
  await page.keyboard.press('Escape')
  await expect(finalMore).toBeFocused()

  const firstRow = page.getByRole('article', { name: /Operator 1, user 40000/i })
  await firstRow.getByRole('button', { name: 'Edit Operator 1' }).click()
  const userDialog = page.getByRole('dialog', { name: 'Edit device user' })
  await expect(userDialog).toBeVisible()
  const overlayOrder = await page.evaluate(() => {
    const overlayRoot = document.querySelector<HTMLElement>('#overlay-root')
    const stickyLayers = ['.app-header', '.live-banner', '.app-sidebar', '.users-section-tabs', '.user-directory-head']
      .map((selector) => document.querySelector<HTMLElement>(selector))
      .filter((node): node is HTMLElement => Boolean(node))
    const overlayZ = Number.parseInt(getComputedStyle(overlayRoot as HTMLElement).zIndex, 10)
    return {
      overlayZ,
      highestStickyZ: Math.max(...stickyLayers.map((node) => Number.parseInt(getComputedStyle(node).zIndex, 10) || 0)),
      dialogInsideOverlay: Boolean(document.querySelector('.dialog-backdrop')?.closest('#overlay-root')),
    }
  })
  expect(overlayOrder.dialogInsideOverlay).toBe(true)
  expect(overlayOrder.overlayZ).toBeGreaterThan(overlayOrder.highestStickyZ)
  await expect(page.locator('#root')).toHaveAttribute('inert', '')
  await page.getByRole('button', { name: 'Close', exact: true }).click()
})

test('old addresses and bookmarks open the new pages with their filters kept', async ({ page }) => {
  const redirects: Array<[string, RegExp]> = [
    ['/fleet', /\/home$/],
    ['/fleet/connector-one', /\/devices\/connector-one$/],
    ['/users/connector-one', /\/people\/connector-one$/],
    ['/alerts?division=life', /\/problems\?division=life$/],
    ['/reconciliation?tab=exceptions', /\/recover\?tab=unreadable$/],
    ['/reconciliation?tab=source-exceptions&device_id=connector-one', /\/recover\?tab=unreadable&device_id=connector-one$/],
    ['/firmware?tab=prepare', /\/setup$/],
    ['/firmware?tab=campaigns', /\/updates\?tab=updates$/],
    ['/#users', /\/people$/],
    ['/no-such-page', /\/home$/],
  ]
  for (const [from, to] of redirects) {
    await page.goto(from)
    await expect(page).toHaveURL(to)
    await expect(page.locator('.app-header')).toBeVisible()
    expect(page.url()).not.toMatch(/cnic|password|reason|confirmation/i)
    expect(page.url()).not.toMatch(/\d{6,}/)
  }
  await expectOnlyAllowedStorage(page)
})

test('frame controls are at least 44 px on touch screens and use plain words', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'phone-390-touch', 'Touch sizes are checked on the phone project.')
  await page.goto('/home')
  await expect(page.getByRole('heading', { name: 'Home', exact: true, level: 1 })).toBeVisible()
  const small = async (selector: string) => page.evaluate((root) => Array.from(document.querySelectorAll<HTMLElement>(`${root} :is(a[href], button, [role="radio"])`))
    .filter((element) => {
      if (element.closest('.skip-link') || element.classList.contains('skip-link')) return false
      const box = element.getBoundingClientRect()
      const style = getComputedStyle(element)
      return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && (box.width < 44 || box.height < 44)
    })
    .map((element) => `${element.tagName} "${(element.getAttribute('aria-label') || element.textContent || '').trim().slice(0, 30)}" ${Math.round(element.getBoundingClientRect().width)}x${Math.round(element.getBoundingClientRect().height)}`), selector)
  expect(await small('.app-header')).toEqual([])
  expect(await small('.app-sidebar')).toEqual([])
  await page.getByRole('navigation', { name: 'Main menu' }).getByRole('button', { name: 'More', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'More' })).toBeVisible()
  expect(await small('.more-sheet')).toEqual([])
  const words = await reportPlainWords(page, testInfo, 'more-sheet')
  expect(words).toEqual([])
})
