import { cleanup, fireEvent, render, screen } from '@testing-library/react'
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
it('keeps previous repair history readable without start or sync controls', async () => {
  const fetch = vi.fn(async (path: string) => response(path.includes('/items') ? { rows: [], next_cursor: null } : { enabled: false, rows: [check], next_cursor: null }))
  vi.stubGlobal('fetch', fetch)
  render(<SafeAttendanceRepair devices={[]} toast={toast} />)
  await screen.findByRole('heading', { name: 'Previous repair history' })
  await screen.findByRole('button', { name: /Checked/ })
  expect(screen.queryByRole('button', { name: /Start repair|Sync and check/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /Checked/ }))
  await screen.findByRole('heading', { name: /admin/ })
  expect(fetch.mock.calls.every(([path]) => !path.endsWith('/start'))).toBe(true)
})
