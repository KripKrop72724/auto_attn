// WORDS-11 for every page: each route is rendered with the shared fixtures
// (which carry raw codes, MACs, messages and digests the way the live API
// sends them) and everything a person can read must pass the plain-words
// guard. Engineer text is allowed only inside an element marked
// data-technical (Technical details), data-brand, data-device-code,
// data-user-text, data-typed-phrase or data-old-name.
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import App from './App'
import { appReady, useDashboardStubs } from './test/renderApp'
import { plainTextProblems } from './words/testing'

const ROUTES: ReadonlyArray<{ path: string; heading: string | RegExp }> = [
  { path: '/home', heading: 'Home' },
  { path: '/devices', heading: 'Devices' },
  { path: '/attendance', heading: 'Attendance' },
  { path: '/problems', heading: 'Problems' },
  { path: '/people', heading: 'People' },
  { path: '/people/connector-one', heading: 'People' },
  { path: '/recover', heading: 'Recover attendance' },
  { path: '/updates', heading: 'Software updates' },
  { path: '/setup', heading: 'Set up a device' },
  { path: '/settings', heading: 'Settings' },
]

/** Lets the page's first answers arrive and its loading placeholders go. */
const settle = async () => {
  await new Promise((resolve) => setTimeout(resolve, 400))
  await waitFor(() => expect(document.querySelectorAll('.ui-skeleton[aria-busy="true"], [data-loading="true"]').length).toBe(0), { timeout: 4000 }).catch(() => undefined)
}

const report = (findings: ReturnType<typeof plainTextProblems>) =>
  findings.slice(0, 20).map((finding) => `${finding.why}: "${finding.match}" in ${finding.where} "${finding.text.slice(0, 100)}" (${finding.element})`)

describe('plain words on every page (WORDS-11)', () => {
  useDashboardStubs(() => Promise.all([
    import('./features/Home'), import('./features/Devices'), import('./features/Attendance'), import('./features/Problems'),
    import('./features/UsersWorkspace'), import('./features/Reconciliation'), import('./features/Firmware'),
    import('./features/FirmwareProvisioning'), import('./features/Settings'), import('./features/DeviceDrawer'),
  ]))

  it.each(ROUTES)('$path has no system words outside Technical details', async ({ path, heading }) => {
    window.history.replaceState({}, '', path)
    render(<App />)
    await appReady(heading as string)
    await settle()
    // A page that broke would pass the words check with only its error card.
    expect(document.querySelector('.page-error-card')).toBeNull()
    expect(report(plainTextProblems(document.body))).toEqual([])
  }, 30_000)

  it('every tab of the open device has no system words outside Technical details', async () => {
    window.history.replaceState({}, '', '/devices/connector-one')
    render(<App />)
    const dialog = await screen.findByRole('dialog', {}, { timeout: 8000 })
    for (const name of ['Summary', 'Activity', 'Problems', 'People', 'Tools']) {
      fireEvent.click(within(dialog).getByRole('tab', { name: new RegExp(`^${name}`) }))
      await settle()
      expect(document.querySelector('.page-error-card')).toBeNull()
      expect([name, ...report(plainTextProblems(dialog))]).toEqual([name])
    }
  }, 60_000)
})
