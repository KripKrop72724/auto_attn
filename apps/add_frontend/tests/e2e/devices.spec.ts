// The device list and map.
import { expect, test, device, mockDashboard, nationwideDevices } from './fixtures'

test.beforeEach(async ({ page }) => mockDashboard(page))

test('nationwide fleet keeps clustered city beacons stable and location details contextual', async ({ page }, testInfo) => {
  await page.route('**/api/v1/devices*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ rows: nationwideDevices }),
  }))
  await page.route('**/api/v1/overview*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ total: 11, online: 10, offline: 0, degraded: 1, flapping: 0, quarantined_duplicate_serial: 0, open_alerts: 1, active_leases: 0, ords_delivery: { backlog: 0, retrying: 0, blocked_identity: 0, quarantined: 0 } }),
  }))

  await page.goto('/home')
  await expect(page.getByRole('heading', { name: 'Fleet', exact: true, level: 1 })).toBeVisible()
  const markers = page.locator('.fleet-map-marker')
  await expect(markers).toHaveCount(7, { timeout: 10_000 })
  const projectedCoordinates = await markers.evaluateAll((nodes) => Object.fromEntries(nodes.map((node) => {
    const location = Array.from(node.classList).find((name) => name.startsWith('location-'))?.replace('location-', '') || ''
    const style = (node as HTMLElement).style
    return [location, {
      x: Number.parseFloat(style.getPropertyValue('--marker-x')),
      y: Number.parseFloat(style.getPropertyValue('--marker-y')),
    }]
  })))
  const expectedCoordinates = {
    swat: { x: 69.21, y: 20.98 },
    peshawar: { x: 64.50, y: 25.91 },
    islamabad: { x: 73.09, y: 28.07 },
    faisalabad: { x: 73.58, y: 42.65 },
    quetta: { x: 38.85, y: 50.95 },
    multan: { x: 64.50, y: 51.09 },
    karachi: { x: 39.00, y: 85.67 },
  }
  for (const [location, expected] of Object.entries(expectedCoordinates)) {
    expect(projectedCoordinates[location].x).toBeCloseTo(expected.x, 1)
    expect(projectedCoordinates[location].y).toBeCloseTo(expected.y, 1)
  }

  const markerCores = await page.locator('.fleet-map-marker-core').evaluateAll((nodes) => nodes.map((node) => {
    const box = node.getBoundingClientRect()
    return { x: box.x + box.width / 2, y: box.y + box.height / 2, radius: box.width / 2 }
  }))
  const closestVisibleGap = Math.min(...markerCores.flatMap((left, index) => markerCores.slice(index + 1).map((right) => (
    Math.hypot(left.x - right.x, left.y - right.y) - left.radius - right.radius
  ))))
  expect(closestVisibleGap).toBeGreaterThanOrEqual(-1)

  const peshawar = page.getByRole('button', { name: /Peshawar, 2 devices, Needs attention/i })
  await page.locator('.page-content > section.panel').evaluate(async (node) => {
    await Promise.all(node.getAnimations().map((animation) => animation.finished.catch(() => undefined)))
  })
  const beforeSelection = await peshawar.evaluate((node) => {
    const box = node.getBoundingClientRect()
    const stage = node.closest('.fleet-map-stage')?.getBoundingClientRect()
    return { x: box.x - (stage?.x || 0), y: box.y - (stage?.y || 0) }
  })
  await peshawar.click()
  await expect(page.getByRole('heading', { name: 'Peshawar' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Inspect Peshawar/ })).toHaveCount(2)
  const afterSelection = await peshawar.evaluate((node) => {
    const box = node.getBoundingClientRect()
    const stage = node.closest('.fleet-map-stage')?.getBoundingClientRect()
    return { x: box.x - (stage?.x || 0), y: box.y - (stage?.y || 0) }
  })
  expect(Math.abs(afterSelection.x - beforeSelection.x)).toBeLessThan(1)
  expect(Math.abs(afterSelection.y - beforeSelection.y)).toBeLessThan(1)

  await page.getByRole('button', { name: /Islamabad, 2 devices, All online/i }).click()
  await expect(page.getByRole('heading', { name: 'Islamabad' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Inspect SLICTOWER/ })).toHaveCount(2)
  await page.getByRole('button', { name: /Multan, 2 devices, All online/i }).click()
  await expect(page.getByRole('heading', { name: 'Multan' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Inspect ZONE-MULTAN/ })).toHaveCount(2)
  await page.getByRole('button', { name: /Faisalabad, 2 devices, All online/i }).click()
  await expect(page.getByRole('heading', { name: 'Faisalabad' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Inspect ZONE-FAISALABAD/ })).toHaveCount(2)
  await page.getByRole('button', { name: /Quetta, 1 device, All online/i }).click()
  await expect(page.getByRole('heading', { name: 'Quetta' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Inspect ZONE-QUETTA-01' })).toBeVisible()
  if ((page.viewportSize()?.width || 0) > 920) {
    const canvasBox = await page.locator('.fleet-map-canvas').boundingBox()
    const sheetBox = await page.locator('.fleet-location-sheet').boundingBox()
    expect((sheetBox?.width || 0)).toBeLessThanOrEqual(400)
    expect((sheetBox?.height || 0)).toBeLessThan(canvasBox?.height || 0)
  }
  if (process.env.ADD_VISUAL_QA === '1') await page.screenshot({ path: testInfo.outputPath('nationwide-map.png'), fullPage: true })
})
