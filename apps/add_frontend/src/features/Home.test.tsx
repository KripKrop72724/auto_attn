import { render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { deviceSnapshots } from '../deviceData'
import { resetPrefs } from '../lib/prefs'
import { appReady, device, fetchStub, response, useDashboardStubs, user } from '../test/renderApp'
import type { Device } from '../types'
import { homeHeadline } from '../words/home'
import { plainTextProblems } from '../words/testing'

const fleet: Device[] = [
  { ...device, connector_id: 'tower-3', friendly_name: 'SLIC Tower · 3rd Floor', division: 'health' },
  { ...device, connector_id: 'bld9-3', display_name: 'LF-ZONE-BLD9-03', friendly_name: 'Building 9 · Device 3', division: 'life', state: 'DEGRADED', health: { mode: 'ENFORCED', tier: 'DEGRADED', primary: { code: 'ESP_DURABILITY_FAULT', since: '2026-11-20T04:24:00Z' } } as Device['health'] },
  { ...device, connector_id: 'multan-2', display_name: 'ZONE-MULTAN-02', friendly_name: 'Multan · Device 2', division: 'health', state: 'OFFLINE', connected: false, last_seen_at: '2026-11-19T12:41:00Z' },
  { ...device, connector_id: 'spare-1', display_name: 'LF-ZONE-BLD1-01', friendly_name: 'Building 1 · Spare', division: 'life', is_spare: true },
]

const tileTexts = () => within(screen.getByRole('region', { name: 'Summary' })).getAllByRole('link').map((tile) => tile.textContent || '')

describe('Home', () => {
  useDashboardStubs(() => import('./Home'))
  beforeEach(() => {
    deviceSnapshots.clear()
    resetPrefs()
  })

  it('shows placeholders, never a 0, before the devices arrive', async () => {
    const base = fetchStub()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
      String(input).startsWith('/api/v1/devices?') ? new Promise<Response>(() => undefined) : base(input, init)))
    render(<App />)
    await appReady()
    const summary = screen.getByRole('region', { name: 'Summary' })
    expect(summary.getAttribute('aria-busy')).toBe('true')
    expect(within(summary).queryByText('0')).toBeNull()
    expect(screen.getByText('Loading devices…')).toBeTruthy()
  })

  it('counts the same rows as the list and shows only devices that are not working', async () => {
    vi.stubGlobal('fetch', fetchStub([user], false, false, false, false, fleet, { active_leases: 0 }))
    render(<App />)
    await appReady()
    await waitFor(() => expect(tileTexts()[0]).toMatch(/^Devices3/))
    expect(tileTexts()[0]).toContain('Life 1 · Health 2')
    expect(tileTexts()[1]).toMatch(/^Working1/)
    expect(tileTexts()[2]).toMatch(/^Needs attention1/)
    expect(tileTexts()[3]).toMatch(/^Offline1/)
    expect(screen.getByText('1 Life device needs attention and 1 Health device is offline.')).toBeTruthy()
    const life = screen.getByRole('region', { name: 'Life' })
    expect(within(life).getByRole('link', { name: /^Open Building 9 · Device 3, / }).textContent).toContain('Link box cannot save attendance safely')
    expect(within(life).getByText('0 of 1 working')).toBeTruthy()
    const health = screen.getByRole('region', { name: 'Health' })
    expect(within(health).getByRole('link', { name: /^Open Multan · Device 2, / }).textContent).toContain('No internet')
    expect(within(health).queryByRole('link', { name: 'Open SLIC Tower · 3rd Floor' })).toBeNull()
    expect(screen.getByRole('link', { name: /^Spare devices \(not in use\): 1 · Life 1 · Health 0/ }).getAttribute('href')).toBe('/devices?spare=1')
    expect(screen.queryByText(/temporary admin access/)).toBeNull()
    // Attendance today, with thousands separators.
    const today = await screen.findByRole('region', { name: 'Attendance today' })
    expect(await within(today).findByText('1,038')).toBeTruthy()
    expect(within(today).getByRole('link', { name: 'Life: 298 sent to the HR system' }).getAttribute('href')).toBe('/attendance?when=today&division=life&status=sent')
    // "SLIC Tower" is a place name, not a code.
    expect(plainTextProblems(document.querySelector('.home-page') as HTMLElement)).toEqual([])
  })

  it('shows Life only when the Division switch says so', async () => {
    vi.stubGlobal('fetch', fetchStub([user], false, false, false, false, fleet))
    window.history.replaceState({}, '', '/home?division=life')
    render(<App />)
    await appReady()
    await waitFor(() => expect(tileTexts()[0]).toMatch(/^Devices1/))
    expect(screen.queryByRole('region', { name: 'Health' })).toBeNull()
    expect(screen.getByRole('region', { name: 'Life' })).toBeTruthy()
    expect(screen.getByText('Health also has 1 offline device.')).toBeTruthy()
    expect(screen.getByText('Showing Life only')).toBeTruthy()
  })

  it('says it is counting while the attendance totals are being prepared, and when there are no new problems', async () => {
    const base = fetchStub([user], false, false, false, false, fleet)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.startsWith('/api/v1/attendance/summary')) return response({ detail: 'Attendance totals are being refreshed.' }, 503)
      if (path.startsWith('/api/v1/alerts')) return response({ rows: [], next_cursor: null, totals: { all: 0, open: 0, acknowledged: 0, resolved: 0 }, queue_totals: { needs_action: 0, acknowledged: 2, resolved: 0, all: 2 } })
      return base(input, init)
    }))
    render(<App />)
    await appReady()
    expect(await screen.findByText("Counting today's check-ins. This takes a few seconds.", undefined, { timeout: 5000 })).toBeTruthy()
    expect(await screen.findByText('No new problems.', { exact: false })).toBeTruthy()
    expect(screen.getByRole('link', { name: /2 seen problems are still open\. See them/ })).toBeTruthy()
  })

  it('writes the headline in plain sentences', () => {
    expect(homeHeadline({ total: 20, attention: 0, offline: 0 })).toBe('All 20 devices are working.')
    expect(homeHeadline({ total: 20, attention: 3, offline: 1 })).toBe('3 devices need attention and 1 is offline.')
    expect(homeHeadline({ total: 20, attention: 0, offline: 1 })).toBe('1 device is offline.')
    expect(homeHeadline({ total: 20, attention: 0, offline: 1, offlineDivision: 'Life' })).toBe('1 Life device is offline.')
    expect(homeHeadline({ total: 1200, attention: 0, offline: 0 })).toBe('All 1,200 devices are working.')
  })
})
