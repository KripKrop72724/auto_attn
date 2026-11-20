import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { AdminSendFacts, ManualForceRelease, type ForceRun } from './ManualForceRelease'

const run: ForceRun = {
  job_id: 'force-run-one', status: 'CHECKED', actor: 'admin', signature: 'a'.repeat(64), execution_enabled: true,
  created_at: '2026-09-23T10:00:00Z', updated_at: '2026-09-23T10:00:00Z', expires_at: '2099-09-23T10:15:00Z', last_error: null,
  counts: { checked: 12, ready: 10, review: 2, waiting: 0, confirmed: 0, stopped: 0, skipped: 0, already_confirmed: 0, already_delivering: 0, unavailable_devices: 1 },
  devices: [{ connector_id: 'active-3fl', name: 'Tower 3', serial: 'PGB1261200074', status: 'CHECKED', checked: 12, ready: 10, synced_at: '2026-09-23T10:00:00Z', error: null },
    { connector_id: 'offline', name: 'Offline tower', serial: 'OFFLINE-SERIAL', status: 'UNAVAILABLE', checked: 0, ready: 0, synced_at: null, error: 'The device is unavailable for a fresh terminal sync.' }],
}
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const toast = { notice: vi.fn(), error: vi.fn() }
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState({}, '', '/attendance?view=fix-tools&tool=held-staff') })
function mock(overrides: Partial<ForceRun> = {}) {
  const fetch = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/items')) return response({ rows: [{ id: 1, status: 'NEEDS_REVIEW', reason: 'CNIC missing', name: 'Example', user_id: '100', device_name: 'Tower 3', device_serial: 'PGB1261200074', time: run.created_at }], next_cursor: null })
    if (path.endsWith('/start')) return response({ ...run, ...overrides, status: 'RUNNING' })
    if (path.endsWith('/attendance-force-releases') && init?.method !== 'POST') return response({ enabled: true, rows: [], next_cursor: null })
    return response({ ...run, ...overrides })
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}
it('requires explicit scope, then one reason and password, while offline devices are excluded', async () => {
  const fetch = mock()
  render(<ManualForceRelease devices={[]} toast={toast} />)
  expect((screen.getByRole('button', { name: 'Re-check not-matched check-ins' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByLabelText('All machines'))
  await waitFor(() => expect((screen.getByRole('button', { name: 'Re-check not-matched check-ins' }) as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Re-check not-matched check-ins' }))
  await screen.findByRole('heading', { name: 'Ready to send' })
  await screen.findByText('CNIC missing')
  expect(screen.queryByRole('checkbox', { name: /Example/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Send 10 check-ins' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByText(/1 machine ready/)).toBeTruthy()
  fireEvent.change(within(dialog).getByLabelText('Why? (saved in the records)'), { target: { value: 'Current employee verified' } })
  fireEvent.change(within(dialog).getByLabelText('Your password'), { target: { value: 'secret' } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Send 10 check-ins' }))
  await screen.findByRole('heading', { name: 'Getting ready' })
  const start = fetch.mock.calls.find(([path]) => path.endsWith('/start'))
  expect(JSON.parse(String(start?.[1]?.body))).toMatchObject({ reason: 'Current employee verified', password: 'secret', signature: run.signature })
  const check = fetch.mock.calls.find(([path, init]) => path.endsWith('/attendance-force-releases') && init?.method === 'POST')
  expect(JSON.parse(String(check?.[1]?.body))).toMatchObject({ scope: 'ALL_PAKISTAN', connector_ids: [] })
  expect(window.location.search).toContain('force_run=force-run-one')
})
it('restores a waiting run and explains stop without claiming completion', async () => {
  window.history.replaceState({}, '', '/attendance?view=fix-tools&tool=held-staff&force_run=force-run-one')
  mock({ status: 'WAITING_ORACLE', counts: { ...run.counts, ready: 0, waiting: 10 } })
  render(<ManualForceRelease devices={[]} toast={toast} />)
  await screen.findByRole('heading', { name: 'Waiting for the HR system' })
  expect(screen.queryByRole('heading', { name: 'Done' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
  expect(screen.getByText(/Check-ins already on the way are still counted/)).toBeTruthy()
  // Stop never sends anything, but keeps its password (owner decision pending).
  expect(within(screen.getByRole('dialog')).getByLabelText('Your password')).toBeTruthy()
})
it('blocks expired approval and never adds Forced to a preview', async () => {
  window.history.replaceState({}, '', '/attendance?view=fix-tools&tool=held-staff&force_run=force-run-one')
  mock({ signature: null })
  render(<ManualForceRelease devices={[]} toast={toast} />)
  await screen.findByRole('heading', { name: 'Ready to send' })
  expect((screen.getByRole('button', { name: 'Send 10 check-ins' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByText('This check expired or belongs to another admin. Check again.')).toBeTruthy()
})
it('shows who sent a check-in by hand, why, and links to the sending task', () => {
  render(<AdminSendFacts evidence={{ run_id: run.job_id, administrator: 'admin', reason: 'Current employee verified', approved_at: run.created_at, audit_id: 12, snapshot_id: 5, sync_command_id: 9 }} />)
  const block = screen.getByRole('region', { name: 'Sent by an admin' })
  expect(block.textContent).toMatch(/Sent by admin on/)
  expect(screen.getByText('Current employee verified')).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Open the sending task' }).getAttribute('href')).toContain('force_run=force-run-one')
  expect(screen.getByText('12').closest('[data-technical]')).toBeTruthy()
})

it('offers All Life and All Health as chosen machines, grouped with friendly names', async () => {
  const fetch = mock()
  const devices = [
    { connector_id: 'life-1', display_name: 'LF-ZONE-BLD9-03', zone_id: 'LF-ZONE-BLD9', friendly_name: 'Building 9 · Device 3', division: 'life', connected: true, hardware_id: 'aa:bb', zkt: { serial: 'S-LIFE' } },
    { connector_id: 'health-1', display_name: 'ZONE-KARACHI-01', zone_id: 'ZONE-KARACHI', friendly_name: 'Karachi · Device 1', division: 'health', connected: false, hardware_id: 'cc:dd', zkt: { serial: 'S-HEALTH' } },
    { connector_id: 'spare-1', display_name: 'ZONE-KARACHI-02', zone_id: 'ZONE-KARACHI', friendly_name: 'Karachi · Device 2', division: 'health', connected: true, hardware_id: 'ee:ff', is_spare: true },
  ] as unknown as Parameters<typeof ManualForceRelease>[0]['devices']
  render(<ManualForceRelease devices={devices} toast={toast} />)
  const picker = screen.getByRole('group', { name: 'Choose machines' })
  expect(within(picker).getByText('Building 9 · Device 3')).toBeTruthy()
  expect(within(picker).queryByText('Karachi · Device 2')).toBeNull()
  expect(picker.textContent).not.toMatch(/aa:bb|S-LIFE/)
  fireEvent.click(screen.getByLabelText('All Life'))
  await waitFor(() => expect((screen.getByRole('button', { name: 'Re-check not-matched check-ins' }) as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Re-check not-matched check-ins' }))
  await screen.findByRole('heading', { name: 'Ready to send' })
  const check = fetch.mock.calls.find(([path, init]) => path.endsWith('/attendance-force-releases') && init?.method === 'POST')
  expect(JSON.parse(String(check?.[1]?.body))).toMatchObject({ scope: 'SELECTED', connector_ids: ['life-1'] })
})
