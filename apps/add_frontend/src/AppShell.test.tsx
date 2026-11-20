import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import App from './App'
import { resetPrefs } from './lib/prefs'
import { appReady, fetchStub, response, useDashboardStubs } from './test/renderApp'
import { expectPlainText } from './words/testing'

const stubPhone = (phone: boolean) => vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
  matches: query.includes('max-width: 760px') ? phone : false,
  media: query,
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
})))

const withProvisioningOff = () => {
  const base = fetchStub()
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    String(input) === '/api/v1/provisioning/capabilities' ? response({ enabled: false, can_start: false }) : base(input, init)))
}

describe('app frame', () => {
  useDashboardStubs(() => import('./features/Home'))

  it('has the everyday menu, the Advanced tools group and plain words', async () => {
    resetPrefs()
    withProvisioningOff()
    render(<App />)
    await appReady()
    const menu = screen.getByRole('navigation', { name: 'Main menu' })
    const labels = Array.from(menu.querySelectorAll('.nav-label')).map((node) => node.textContent)
    expect(labels).toEqual(['Home', 'Devices', 'Attendance', 'Problems', 'People', 'Recover attendance', 'Software updates', 'Set up a device', 'Settings'])
    expect(within(menu).getByRole('group', { name: 'Advanced tools' })).toBeTruthy()
    const home = within(menu).getByRole('link', { name: 'Home' })
    expect(home.getAttribute('href')).toBe('/home')
    expect(home.getAttribute('aria-current')).toBe('page')
    expect(await within(menu).findByText('Switched off')).toBeTruthy()
    // The problem count is said in words inside the link, never as a bare number.
    expect(await within(menu).findByText('1 problem to look at')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Skip to main content' })).toBeTruthy()
    expect(document.querySelector('.live-sync')?.textContent).toMatch(/^(Connecting…|Live|Reconnecting…)$/)
    expectPlainText(document.querySelector('.app-sidebar') as HTMLElement)
    expectPlainText(document.querySelector('.app-header') as HTMLElement)
  })

  it('moves to a page from the menu, keeps the Division choice and focuses the new title', async () => {
    resetPrefs()
    window.history.replaceState({}, '', '/home?division=life')
    render(<App />)
    await appReady()
    const menu = screen.getByRole('navigation', { name: 'Main menu' })
    expect(within(menu).getByRole('link', { name: 'Devices' }).getAttribute('href')).toBe('/devices?division=life')
    fireEvent.click(within(menu).getByRole('link', { name: 'Devices' }))
    const heading = await appReady('Devices')
    expect(window.location.search).toBe('?division=life')
    await waitFor(() => expect(document.activeElement).toBe(heading))
    expect(document.title).toBe('Devices · State Life Attendance')
    // "Show all" on the chip goes back to All.
    fireEvent.click(screen.getByRole('button', { name: 'Show all' }))
    await waitFor(() => expect(window.location.search).toBe(''))
  })

  it('opens the account menu with technical details, Settings and Sign out', async () => {
    resetPrefs()
    render(<App />)
    await appReady()
    fireEvent.click(screen.getByRole('button', { name: 'StateHealthAdmin, account menu' }))
    const menu = screen.getByRole('menu')
    expect(within(menu).getByText('Signed in as StateHealthAdmin')).toBeTruthy()
    expect(within(menu).getByRole('switch', { name: 'Show technical details' })).toBeTruthy()
    expect(within(menu).getByRole('menuitem', { name: /Settings/ }).getAttribute('href')).toBe('/settings')
    expect(within(menu).getByRole('button', { name: /Sign out/ })).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })

  it('on a phone has a five-cell bottom bar and a More sheet with the advanced tools', async () => {
    resetPrefs()
    stubPhone(true)
    withProvisioningOff()
    render(<App />)
    await appReady()
    const menu = screen.getByRole('navigation', { name: 'Main menu' })
    const more = within(menu).getByRole('button', { name: 'More' })
    expect(screen.queryByRole('button', { name: /account menu/ })).toBeNull()
    fireEvent.click(more)
    const sheet = await screen.findByRole('dialog', { name: 'More' })
    for (const name of ['People', 'Recover attendance', 'Software updates', 'Set up a device', 'Settings']) {
      expect(within(sheet).getByRole('link', { name: new RegExp(`^${name}`) })).toBeTruthy()
    }
    expect(within(sheet).getByText('Signed in as StateHealthAdmin')).toBeTruthy()
    expect(within(sheet).getByRole('button', { name: /Sign out/ })).toBeTruthy()
    expect(within(sheet).getByText('All times are Pakistan time')).toBeTruthy()
    expectPlainText(sheet)
    fireEvent.click(within(sheet).getByRole('link', { name: /^Settings/ }))
    await appReady('Settings')
    expect(screen.queryByRole('dialog', { name: 'More' })).toBeNull()
  })
})
