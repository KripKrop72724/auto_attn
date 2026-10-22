import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { Device } from '../types'
import { IdentityBacklogRelease, type BacklogRun } from './IdentityBacklogRelease'

const devices = [
  { connector_id: 'karachi-1', zone_id: 'ZONE-KHI', zone_name: 'Karachi', firmware_family: 'zkt' },
  { connector_id: 'karachi-2', zone_id: 'ZONE-KHI', zone_name: 'Karachi', firmware_family: 'zkt' },
  { connector_id: 'hik-1', zone_id: 'ZONE-HIK', zone_name: 'Hikvision zone', firmware_family: 'hikvision' },
] as unknown as Device[]
const scope = { zone_id: 'ZONE-KHI', as_of: '2026-10-21T10:00:00+00:00', until_id: 90, window_hours: 72, connectors: ['karachi-1', 'karachi-2'] }
const run: BacklogRun = {
  job_id: 'backlog-run-1', zone_id: 'ZONE-KHI', status: 'RELEASING', paused: false, actor: 'StateHealthAdmin',
  reason: 'Reviewed Karachi backlog', connectors: ['karachi-1'], checked_at: scope.as_of, approved: 2, released: 1,
  delivered: 0, waiting_oracle: 1, kept_held: 0, left_to_snapshot: 0, needs_review: 0, waiting_reason: null,
  created_at: '2026-10-21T10:00:00Z', updated_at: '2026-10-21T10:00:00Z', completed_at: null,
}
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const toast = { notice: vi.fn(), error: vi.fn() }
afterEach(() => { cleanup(); vi.unstubAllGlobals(); toast.notice.mockReset() })

function mock({ busyOnce = false, releaseEnabled = true, paused = false, runs = [] as BacklogRun[] } = {}) {
  let busy = busyOnce
  const fetch = vi.fn(async (path: string, init?: RequestInit) => {
    const url = new URL(path, 'https://add.test')
    if (url.pathname.endsWith('/preview')) {
      const first = url.searchParams.get('after_id') === '0'
      if (!first && busy) {
        busy = false
        return response({ detail: { code: 'PREVIEW_BUSY', message: 'Another zone check is running.' } }, 409)
      }
      return response({
        zone_id: 'ZONE-KHI', scanned: first ? 3 : 1, next_after_id: first ? 41 : null, window_hours: 72,
        as_of: scope.as_of, until_id: 90, release_enabled: releaseEnabled, automatic_zone_enabled: false,
        devices: [{ connector_id: 'karachi-1', name: 'Karachi gate' }, { connector_id: 'karachi-2', name: 'Karachi lift' }],
        groups: first
          ? [
            { connector_id: 'karachi-1', source: 'DUMP_RECONNECT', age: 'UP_TO_7_DAYS', held: 2, provable: 1, next_snapshot: 0 },
            { connector_id: 'karachi-2', source: 'DUMP_RECONNECT', age: 'UP_TO_7_DAYS', held: 2, provable: 1, next_snapshot: 1 },
          ]
          : [{ connector_id: 'karachi-1', source: 'DUMP_STARTUP', age: 'OLDER', held: 1, provable: 1, next_snapshot: 0 }],
        ...(first ? {
          held_total: 5, reheld_outside_check: 2,
          approval: { scope, expires_at: '2026-10-21T22:00:00+00:00', signature: 'f'.repeat(64) },
        } : {}),
      })
    }
    if (url.pathname.endsWith('/stop')) return response({ ...run, status: 'STOPPED_FOLLOWING' })
    if (url.pathname.endsWith('/releases') && init?.method === 'POST') return response({ ...run, paused }, 202)
    if (url.pathname.endsWith('/releases')) return response({ rows: runs, confirmation: 'RELEASE ZONE-KHI' })
    return response({ ...run, paused })
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

const checkZone = async (fetch: ReturnType<typeof mock>) => {
  render(<IdentityBacklogRelease devices={devices} toast={toast} />)
  const zone = screen.getByLabelText('Zone')
  expect(within(zone).queryByText('Hikvision zone')).toBeNull()
  fireEvent.change(zone, { target: { value: 'ZONE-KHI' } })
  fireEvent.click(screen.getByRole('button', { name: 'Check zone' }))
  await screen.findByRole('table', {}, { timeout: 4000 })
  return fetch.mock.calls.filter(([path]) => String(path).includes('/preview'))
    .map(([path]) => new URL(String(path), 'https://add.test').searchParams)
}

it('checks a frozen zone page by page, then releases only the chosen groups', async () => {
  const fetch = mock()
  const previews = await checkZone(fetch)
  expect(previews.map((params) => params.get('after_id'))).toEqual(['0', '41'])
  // Later pages carry the frozen check, so every page counts the same punches.
  expect(previews[0].get('as_of')).toBeNull()
  expect(Object.fromEntries(previews[1])).toMatchObject({ as_of: scope.as_of, until_id: '90', window_hours: '72' })
  const table = screen.getByRole('table')
  expect(within(table).getAllByText('Karachi gate')).toHaveLength(2)
  expect(within(table).getByText('Over 30 days')).toBeTruthy()
  expect(screen.getByText('Stay held').closest('.metric-card')?.textContent).toContain('1')
  // Another proof releases one of them at the next user list, not this action.
  expect(screen.getByText('Next user list', { selector: '.metric-card *' }).closest('.metric-card')?.textContent).toContain('1')
  screen.getByText('2 punches released earlier and held again by delivery are not part of this check. The release or user list that sent them checks them again.')
  screen.getByRole('button', { name: 'Release 3 punches' })

  // Leave out the over-30-days group of the gate terminal.
  fireEvent.click(screen.getByRole('checkbox', { name: 'Release Karachi gate, Startup recovery, Over 30 days' }))
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
  expect(body).toMatchObject({
    zone_id: 'ZONE-KHI', reason: 'Reviewed Karachi backlog', typed_confirmation: 'RELEASE ZONE-KHI', password: 'secret',
    scope, signature: 'f'.repeat(64), expires_at: '2026-10-21T22:00:00+00:00', approved_count: 2, previewed_held: 4,
  })
  expect(body.groups).toEqual([
    { connector_id: 'karachi-1', source: 'DUMP_RECONNECT', age: 'UP_TO_7_DAYS' },
    { connector_id: 'karachi-2', source: 'DUMP_RECONNECT', age: 'UP_TO_7_DAYS' },
  ])
  expect(body.idempotency_key).toMatch(/^identity-backlog:/)
  expect(screen.getByRole('status').textContent).toContain('0 confirmed by Oracle · 1 waiting for Oracle · 1 of 2 approved released')

  fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
  const stop = screen.getByRole('dialog')
  fireEvent.change(within(stop).getByLabelText('Administrator password'), { target: { value: 'secret' } })
  expect(stop.textContent).toContain('ADD keeps following them to Oracle')
  fireEvent.click(within(stop).getByRole('button', { name: 'Stop release' }))
  await screen.findByRole('heading', { name: 'Stopped: following released punches to Oracle' })
  // No Stop again, and no new approval while it still follows its punches.
  expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
  expect((screen.getByRole('button', { name: /^Release \d+ punch/ }) as HTMLButtonElement).disabled).toBe(true)
  await waitFor(() => expect(toast.notice).toHaveBeenCalledTimes(2))
}, 20000)

it('waits for another zone check instead of failing', async () => {
  const fetch = mock({ busyOnce: true })
  const previews = await checkZone(fetch)
  expect(previews.map((params) => params.get('after_id'))).toEqual(['0', '41', '41'])
  screen.getByRole('button', { name: 'Release 3 punches' })
})

it('cannot approve while the server has the release proof turned off', async () => {
  const fetch = mock({ releaseEnabled: false })
  await checkZone(fetch)
  expect(screen.getByRole('alert').textContent).toContain('turned off on this server')
  expect((screen.getByRole('button', { name: 'Release 3 punches' }) as HTMLButtonElement).disabled).toBe(true)
})

it('shows why a running release waits and what it left to the next user list', async () => {
  mock({ runs: [{ ...run, waiting_reason: 'WAITING_FOR_COMPLETE_ROSTER', left_to_snapshot: 3 }] })
  render(<IdentityBacklogRelease devices={devices} toast={toast} />)
  fireEvent.change(screen.getByLabelText('Zone'), { target: { value: 'ZONE-KHI' } })
  const status = await screen.findByRole('status')
  expect(status.textContent).toContain('3 left to the next user list')
  expect(status.textContent).toContain('Waiting for a complete user list from a chosen terminal')
})
