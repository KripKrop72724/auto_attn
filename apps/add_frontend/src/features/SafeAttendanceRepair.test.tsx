import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { SafeAttendanceRepair, type RepairRun } from './SafeAttendanceRepair'

const check: RepairRun = {
  job_id: '11111111-1111-4111-8111-111111111111', status: 'CHECKED', actor: 'admin',
  counts: { checked: 12, ready: 10, waiting: 0, confirmed: 0, review: 2, stopped: 0 },
  check_complete: true, created_at: '2026-09-22T10:00:00Z', updated_at: '2026-09-22T10:00:00Z',
  expires_at: '2099-09-22T10:15:00Z', signature: 'a'.repeat(64), last_error: null,
  execution_enabled: true, eligible_at_check: 10,
  devices: [{ connector_id: 'c1', name: 'Tower 3', serial: 'PGB1261200074', checked: 12, status: 'CHECKED' }],
}
const toast = { notice: vi.fn(), error: vi.fn() }
const response = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState({}, '', '/attendance?view=needs-review') })
function mock(overrides: Partial<RepairRun> = {}) {
  const fetch = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/coverage')) return response({ total: 30, confirmed: 18, pending: 0, identity_held: 12, review: 0, observed_at: check.updated_at })
    if (path.includes('/items')) return response({ rows: [], next_cursor: null })
    if (path.endsWith('/jobs')) return response({ ...check, ...overrides, status: 'RUNNING' })
    if (path.endsWith('/checks') && init?.method === 'POST') return response({ ...check, ...overrides })
    if (path.includes('/checks/')) return response({ ...check, ...overrides })
    return response({ enabled: true, rows: [], next_cursor: null })
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}
it('uses a short check then password approval without typed technical commands', async () => {
  const fetch = mock()
  render(<SafeAttendanceRepair devices={[]} toast={toast} />)
  fireEvent.click(screen.getByRole('button', { name: 'Repair attendance' }))
  const checkButton = screen.getByRole('button', { name: 'Check saved attendance' })
  await waitFor(() => expect((checkButton as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(checkButton)
  await screen.findByRole('heading', { name: 'Ready to review' })
  expect(screen.getByText('10 punches can be repaired. 2 need more evidence. Review the device list, then start.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Start repair' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).queryByText(/type.*recover/i)).toBeNull()
  fireEvent.change(within(dialog).getByLabelText('Administrator password'), { target: { value: 'secret' } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Start repair' }))
  await screen.findByRole('heading', { name: 'Repair in progress' })
  const request = fetch.mock.calls.find(([path]) => path.endsWith('/jobs'))
  expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ action: 'SAFE_REPAIR', check_id: check.job_id, password: 'secret' })
  expect(window.location.search).toContain(`repair_run=${check.job_id}`)
})
it('reloads a saved run and never treats queued attendance as confirmed', async () => {
  window.history.replaceState({}, '', `/attendance?view=needs-review&repair_run=${check.job_id}`)
  mock({ status: 'WAITING_ORACLE', counts: { ...check.counts, ready: 0, waiting: 10, confirmed: 0 } })
  render(<SafeAttendanceRepair devices={[]} toast={toast} />)
  await screen.findByRole('heading', { name: 'Waiting for Oracle' })
  expect(screen.queryByText('Repair complete')).toBeNull()
  expect(screen.getByText('You can leave this page. Progress is saved.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Stop repair' }))
  expect(screen.getByText('Stop adding records to delivery. Records already queued will continue to Oracle.')).toBeTruthy()
})
it('keeps expired and check-only runs unavailable for execution', async () => {
  window.history.replaceState({}, '', `/attendance?view=needs-review&repair_run=${check.job_id}`)
  mock({ signature: null, execution_enabled: false })
  render(<SafeAttendanceRepair devices={[]} toast={toast} />)
  await screen.findByRole('heading', { name: 'Ready to review' })
  expect((screen.getByRole('button', { name: 'Start repair' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByText(/check expired or belongs to another admin/i)).toBeTruthy()
})
