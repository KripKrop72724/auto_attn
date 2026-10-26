import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

const hoursAgo = (hours: number) => new Date(Date.now() - hours * 3_600_000).toISOString()
const reasons = {
  no_internet: ['No internet', 'The device may be switched off, or the internet at this place is not working.'],
  machine_not_responding: ['Machine not responding', 'The attendance machine may be switched off or its cable unplugged.'],
  machine_records_changed: ['Machine records changed', "The attendance machine's saved records have changed, for example after a reset, so it needs to be checked."],
} as const
const device = (code: string, name: string, zone: 'life' | 'health', status: 'online' | 'offline', since: string,
  reason: keyof typeof reasons | null = null) =>
  ({ code, name, location: name.split(' · ')[0], unit: name.split(' · ')[1] ?? null, zone, status, since,
     reason_code: reason, reason: reason && reasons[reason][0], reason_detail: reason && reasons[reason][1] })

const devices = [
  device('LF-ZONE-BLD1-01', 'Building 1', 'life', 'offline', hoursAgo(29), 'machine_records_changed'),
  device('LF-ZONE-BLD5-01', 'Building 5', 'life', 'online', hoursAgo(4.5)),
  device('LF-ZONE-BLD9-01', 'Building 9 · Device 1', 'life', 'offline', hoursAgo(22), 'no_internet'),
  device('LF-ZONE-BLD9-02', 'Building 9 · Device 2', 'life', 'offline', hoursAgo(22), 'no_internet'),
  device('LF-ZONE-BLD9-03', 'Building 9 · Device 3', 'life', 'offline', hoursAgo(73), 'machine_not_responding'),
  device('LF-ZONE-G&PBLD8-01', 'Group & Pension Building 8', 'life', 'online', hoursAgo(4.5)),
  device('ZONE-BLD9-01', 'Building 9', 'health', 'online', hoursAgo(4.5)),
  device('ZONE-FAISALABAD-01', 'Faisalabad · Device 1', 'health', 'online', hoursAgo(4.5)),
  device('ZONE-FAISALABAD-02', 'Faisalabad · Device 2', 'health', 'online', hoursAgo(4.5)),
  device('ZONE-KARACHI-01', 'Karachi', 'health', 'online', hoursAgo(4.5)),
  device('ZONE-LAHORE-01', 'Lahore · Device 1', 'health', 'online', hoursAgo(4.5)),
  device('ZONE-LAHORE-02', 'Lahore · Device 2', 'health', 'online', hoursAgo(1.8)),
  device('ZONE-MULTAN-01', 'Multan · Device 1', 'health', 'online', hoursAgo(4)),
  device('ZONE-MULTAN-02', 'Multan · Device 2', 'health', 'offline', hoursAgo(25), 'no_internet'),
  device('ZONE-PESHAWAR-02', 'Peshawar · Device 2', 'health', 'online', hoursAgo(4.5)),
  device('ZONE-PESHAWAR-06', 'Peshawar · Device 6', 'health', 'online', hoursAgo(4.5)),
  device('ZONE-QUETTA-01', 'Quetta', 'health', 'online', hoursAgo(4.5)),
  device('ZONE-SLICTOWER-3FL', 'SLIC Tower · 3rd Floor', 'health', 'online', hoursAgo(4.5)),
  device('ZONE-SLICTOWER-13FL', 'SLIC Tower · 13th Floor', 'health', 'online', hoursAgo(4.5)),
  device('ZONE-SWAT-01', 'Swat', 'health', 'online', hoursAgo(4.5)),
]
const zone = (key: 'life' | 'health', name: string) => {
  const members = devices.filter((row) => row.zone === key)
  const online = members.filter((row) => row.status === 'online').length
  return { zone: key, name, total: members.length, online, offline: members.length - online }
}
const snapshot = {
  updated_at: new Date().toISOString(),
  total: devices.length,
  online: devices.filter((row) => row.status === 'online').length,
  offline: devices.filter((row) => row.status === 'offline').length,
  zones: [zone('life', 'Life'), zone('health', 'Health')],
  devices,
}

test('public status page needs no sign-in, fits every screen and is accessible', async ({ page }, testInfo) => {
  const dashboardCalls: string[] = []
  await page.route('**/events/**', (route) => route.abort())
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/public/v1/status') return route.fulfill({ json: snapshot })
    dashboardCalls.push(url.pathname)
    return route.fulfill({ status: 401, json: { detail: 'Authentication required.' } })
  })
  await page.goto('/status')

  await expect(page.getByRole('heading', { level: 1, name: 'Attendance Device Status' })).toBeVisible()
  const life = page.getByRole('region', { name: 'Life' })
  await expect(life.getByText('2 of 6 online')).toBeVisible()
  await expect(life.getByText('4 offline')).toBeVisible()
  await expect(life.getByRole('listitem').filter({ hasText: 'Building 9 · Device 3' }))
    .toContainText('Machine not responding. The attendance machine may be switched off or its cable unplugged.')
  await expect(page.getByRole('region', { name: 'Health' }).getByText('13 of 14 online')).toBeVisible()
  await expect(page.getByRole('button', { name: /sign in/i })).toHaveCount(0)
  expect(dashboardCalls).toEqual([])

  const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }))
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(results.violations).toEqual([])
  const screenshot = testInfo.outputPath('public-status.png')
  await page.screenshot({ path: screenshot, fullPage: true })
  await testInfo.attach('public-status', { path: screenshot, contentType: 'image/png' })
})
