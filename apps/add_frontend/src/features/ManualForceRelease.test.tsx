import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ManualForceRelease, ForcedPill, type ForceRun } from './ManualForceRelease'

const run: ForceRun = {
  job_id: 'force-run-one', status: 'CHECKED', actor: 'admin', signature: 'a'.repeat(64), execution_enabled: true,
  created_at: '2026-09-23T10:00:00Z', updated_at: '2026-09-23T10:00:00Z', expires_at: '2099-09-23T10:15:00Z', last_error: null,
  counts: { checked: 12, ready: 10, review: 2, waiting: 0, confirmed: 0, stopped: 0, skipped: 0, already_confirmed: 0, already_delivering: 0, unavailable_devices: 1 },
  devices: [{ connector_id: 'active-3fl', name: 'Tower 3', serial: 'PGB1261200074', status: 'CHECKED', checked: 12, ready: 10, synced_at: '2026-09-23T10:00:00Z', error: null },
    { connector_id: 'offline', name: 'Offline tower', serial: 'OFFLINE-SERIAL', status: 'UNAVAILABLE', checked: 0, ready: 0, synced_at: null, error: 'The device is unavailable for a fresh terminal sync.' }],
}
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const toast = { notice: vi.fn(), error: vi.fn() }
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState({}, '', '/attendance?view=needs-review') })
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
  expect((screen.getByRole('button', { name: 'Sync and check' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByLabelText('All Pakistan'))
  await waitFor(() => expect((screen.getByRole('button', { name: 'Sync and check' }) as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Sync and check' }))
  await screen.findByRole('heading', { name: 'Ready to review' })
  await screen.findByText('CNIC missing')
  expect(screen.queryByRole('checkbox', { name: /Example/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Force release 10 punches' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByText(/1 ready devices/)).toBeTruthy()
  fireEvent.change(within(dialog).getByLabelText('Reason for release'), { target: { value: 'Current employee verified' } })
  fireEvent.change(within(dialog).getByLabelText('Administrator password'), { target: { value: 'secret' } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Approve 10 punches' }))
  await screen.findByRole('heading', { name: 'Preparing delivery' })
  const start = fetch.mock.calls.find(([path]) => path.endsWith('/start'))
  expect(JSON.parse(String(start?.[1]?.body))).toMatchObject({ reason: 'Current employee verified', password: 'secret', signature: run.signature })
  const check = fetch.mock.calls.find(([path, init]) => path.endsWith('/attendance-force-releases') && init?.method === 'POST')
  expect(JSON.parse(String(check?.[1]?.body))).toMatchObject({ scope: 'ALL_PAKISTAN', connector_ids: [] })
  expect(window.location.search).toContain('force_run=force-run-one')
})
it('restores a waiting run and explains stop without claiming completion', async () => {
  window.history.replaceState({}, '', '/attendance?view=needs-review&force_run=force-run-one')
  mock({ status: 'WAITING_ORACLE', counts: { ...run.counts, ready: 0, waiting: 10 } })
  render(<ManualForceRelease devices={[]} toast={toast} />)
  await screen.findByRole('heading', { name: 'Waiting for Oracle' })
  expect(screen.queryByRole('heading', { name: 'Finished' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
  expect(screen.getByText(/Records already queued continue to be accounted for/)).toBeTruthy()
})
it('blocks expired approval and never adds Forced to a preview', async () => {
  window.history.replaceState({}, '', '/attendance?view=needs-review&force_run=force-run-one')
  mock({ signature: null })
  render(<ManualForceRelease devices={[]} toast={toast} />)
  await screen.findByRole('heading', { name: 'Ready to review' })
  expect((screen.getByRole('button', { name: 'Force release 10 punches' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.queryByRole('button', { name: /Forced attendance/ })).toBeNull()
})
it('opens the committed manual decision independently of delivery status', () => {
  render(<ForcedPill evidence={{ run_id: run.job_id, administrator: 'admin', reason: 'Current employee verified', approved_at: run.created_at, audit_id: 12, snapshot_id: 5, sync_command_id: 9 }} />)
  fireEvent.click(screen.getByRole('button', { name: /Forced attendance/ }))
  expect(screen.getByRole('dialog')).toBeTruthy()
  expect(screen.getByText('Current employee verified')).toBeTruthy()
  expect(screen.getByRole('link', { name: /Open saved run/ }).getAttribute('href')).toContain('force_run=force-run-one')
})
