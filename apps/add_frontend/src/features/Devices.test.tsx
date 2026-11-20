import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import App from '../App'
import { appReady, device, fetchStub, useDashboardStubs, user } from '../test/renderApp'
import type { Device } from '../types'
import { plainTextProblems } from '../words/testing'

// "SLIC Tower" is a place name (State Life Insurance Corporation), not a code.
const expectPlain = (root: Element) => expect(plainTextProblems(root)).toEqual([])

const fleet: Device[] = [
  { ...device, connector_id: 'tower-13', display_name: 'ZONE-SLICTOWER-13FL', friendly_name: 'SLIC Tower · 13th Floor', division: 'health', zkt: { ...device.zkt!, serial: 'ISB-0013' } },
  { ...device, connector_id: 'tower-3', friendly_name: 'SLIC Tower · 3rd Floor', division: 'health' },
  { ...device, connector_id: 'connector-one', display_name: 'LF-ZONE-BLD9-03', zone_id: 'LF-ZONE-BLD9-03', friendly_name: 'Building 9 · Device 3', division: 'life', state: 'DEGRADED', health: { mode: 'ENFORCED', tier: 'DEGRADED', primary: { code: 'ESP_DURABILITY_FAULT', since: '2026-11-20T04:24:00Z' } } as Device['health'] },
  { ...device, connector_id: 'multan-2', display_name: 'ZONE-MULTAN-02', zone_id: 'ZONE-MULTAN-02', friendly_name: 'Multan · Device 2', division: 'health', state: 'OFFLINE', connected: false },
  { ...device, connector_id: 'spare-1', display_name: 'LF-ZONE-BLD1-01', friendly_name: 'Building 1 · Spare', division: 'life', is_spare: true },
]

const names = () => screen.getAllByRole('link').map((link) => link.getAttribute('aria-label') || '').filter((label) => label.startsWith('Open '))

describe('Devices list', () => {
  useDashboardStubs(() => Promise.all([import('./Devices'), import('./DeviceDrawer')]))

  const open = async (path = '/devices', first: RegExp = /^Open /) => {
    vi.stubGlobal('fetch', fetchStub([user], false, false, false, false, fleet))
    window.history.replaceState({}, '', path)
    render(<App />)
    await appReady('Devices')
    await screen.findAllByRole('link', { name: first })
  }

  it('lists needs attention first, numbers in order, with plain words only', async () => {
    await open()
    expect(names()).toEqual([
      'Open Multan · Device 2, Health, offline',
      'Open Building 9 · Device 3, Life, needs attention',
      'Open SLIC Tower · 3rd Floor, Health, working',
      'Open SLIC Tower · 13th Floor, Health, working',
    ])
    const row = screen.getByRole('link', { name: /^Open Building 9/ }).closest('tr') as HTMLElement
    expect(row.textContent).toContain('Link box cannot save attendance safely.')
    expect(within(row).getByText('Life')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'People on Building 9 · Device 3' }).getAttribute('href')).toBe('/people/connector-one')
    expect(document.body.textContent).not.toContain('e0:72:a1')
    expectPlain(document.querySelector('.devices-page') as HTMLElement)
  })

  it('writes each filter to the address and reads it back', async () => {
    await open()
    fireEvent.click(screen.getByRole('button', { name: /^Offline\s*1$/ }))
    await waitFor(() => expect(window.location.search).toBe('?status=offline'))
    expect(names()).toEqual(['Open Multan · Device 2, Health, offline'])
    expect(screen.getByText('Showing 1 of 4')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /^Spare\s*1$/ }))
    await waitFor(() => expect(window.location.search).toContain('spare=1'))
    expect(names()).toEqual(['Open Building 1 · Spare, Life, spare (not in use)'])
  })

  it('opens a shared filtered address as it was', async () => {
    await open('/devices?q=isb-0013')
    await waitFor(() => expect(names()).toEqual(['Open SLIC Tower · 13th Floor, Health, working']))
  })

  it('follows the Division choice and searches by division word', async () => {
    await open('/devices?division=life')
    await waitFor(() => expect(names()).toEqual(['Open Building 9 · Device 3, Life, needs attention']))
    expect(screen.getByText('Showing Life only')).toBeTruthy()
  })

  it('keeps the filters when a device is opened and closed', async () => {
    await open('/devices?status=attention')
    fireEvent.click(await screen.findByRole('link', { name: /^Open Building 9/ }))
    await waitFor(() => expect(window.location.pathname).toBe('/devices/connector-one'))
    expect(await screen.findByRole('dialog', undefined, { timeout: 8000 })).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(window.location.pathname).toBe('/devices'))
    expect(window.location.search).toBe('?status=attention')
  })
})
