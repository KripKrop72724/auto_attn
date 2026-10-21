import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { Device } from '../types'
import { IdentityBacklogRelease, type BacklogRun } from './IdentityBacklogRelease'

const devices = [
  { connector_id: 'karachi-1', zone_id: 'ZONE-KHI', zone_name: 'Karachi', firmware_family: 'zkt' },
  { connector_id: 'hik-1', zone_id: 'ZONE-HIK', zone_name: 'Hikvision zone', firmware_family: 'hikvision' },
] as unknown as Device[]
const run: BacklogRun = {
  job_id: 'backlog-run-1', zone_id: 'ZONE-KHI', status: 'RELEASING', actor: 'StateHealthAdmin',
  reason: 'Reviewed Karachi backlog', connectors: ['karachi-1'], held_at_approval: 3, released: 0, kept_held: 0,
  created_at: '2026-10-21T10:00:00Z', updated_at: '2026-10-21T10:00:00Z', completed_at: null,
}
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const toast = { notice: vi.fn(), error: vi.fn() }
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function mock() {
  const fetch = vi.fn(async (path: string, init?: RequestInit) => {
    const url = new URL(path, 'https://add.test')
    if (url.pathname.endsWith('/preview')) {
      const first = url.searchParams.get('after_id') === '0'
      return response({
        zone_id: 'ZONE-KHI', scanned: first ? 2 : 1, next_after_id: first ? 41 : null, window_hours: 72,
        groups: [first
          ? { source: 'DUMP_RECONNECT', age: 'UP_TO_7_DAYS', held: 2, provable: 1 }
          : { source: 'DUMP_STARTUP', age: 'OLDER', held: 1, provable: 1 }],
        ...(first ? { held_total: 3 } : {}),
      })
    }
    if (url.pathname.endsWith('/stop')) return response({ ...run, status: 'STOPPED', released: 1 })
    if (url.pathname.endsWith('/releases') && init?.method === 'POST') return response(run, 202)
    if (url.pathname.endsWith('/releases')) return response({ rows: [], confirmation: 'RELEASE ZONE-KHI' })
    return response(run)
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

it('checks a zone page by page, then needs a reason, typed zone and password to release', async () => {
  const fetch = mock()
  render(<IdentityBacklogRelease devices={devices} toast={toast} />)
  const zone = screen.getByLabelText('Zone')
  expect(within(zone).queryByText('Hikvision zone')).toBeNull()
  fireEvent.change(zone, { target: { value: 'ZONE-KHI' } })
  fireEvent.click(screen.getByRole('button', { name: 'Check zone' }))
  await screen.findByRole('button', { name: 'Release 2 punches' })
  const previews = fetch.mock.calls.filter(([path]) => String(path).includes('/preview'))
  expect(previews.map(([path]) => new URL(String(path), 'https://add.test').searchParams.get('after_id'))).toEqual(['0', '41'])
  const table = screen.getByRole('table')
  expect(within(table).getByText('Reconnect recovery')).toBeTruthy()
  expect(within(table).getByText('Over 30 days')).toBeTruthy()
  expect(screen.getByText('Stay held').closest('.metric-card')?.textContent).toContain('1')

  fireEvent.click(screen.getByRole('button', { name: 'Release 2 punches' }))
  const dialog = screen.getByRole('dialog')
  const approve = within(dialog).getByRole('button', { name: 'Approve 2 punches' }) as HTMLButtonElement
  fireEvent.change(within(dialog).getByLabelText('Reason for release'), { target: { value: 'Reviewed Karachi backlog' } })
  fireEvent.change(within(dialog).getByLabelText('Administrator password'), { target: { value: 'secret' } })
  fireEvent.change(within(dialog).getByLabelText('Type RELEASE ZONE-KHI to confirm'), { target: { value: 'RELEASE' } })
  expect(approve.disabled).toBe(true)
  fireEvent.change(within(dialog).getByLabelText('Type RELEASE ZONE-KHI to confirm'), { target: { value: 'RELEASE ZONE-KHI' } })
  expect(approve.disabled).toBe(false)
  fireEvent.click(approve)
  await screen.findByRole('heading', { name: 'Releasing in batches' })
  const post = fetch.mock.calls.find(([path, init]) => String(path).endsWith('/releases') && init?.method === 'POST')
  const body = JSON.parse(String(post?.[1]?.body))
  expect(body).toMatchObject({ zone_id: 'ZONE-KHI', reason: 'Reviewed Karachi backlog', typed_confirmation: 'RELEASE ZONE-KHI', password: 'secret' })
  expect(body.idempotency_key).toMatch(/^identity-backlog:/)

  fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
  const stop = screen.getByRole('dialog')
  fireEvent.change(within(stop).getByLabelText('Administrator password'), { target: { value: 'secret' } })
  fireEvent.click(within(stop).getByRole('button', { name: 'Stop release' }))
  await screen.findByRole('heading', { name: 'Stopped' })
  await waitFor(() => expect(toast.notice).toHaveBeenCalledTimes(2))
})
