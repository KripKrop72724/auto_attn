import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Device, FirmwareCampaign, FirmwareRelease } from '../types'
import { FirmwareView } from './Firmware'

const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const campaign = (changes: Partial<FirmwareCampaign>): FirmwareCampaign => ({
  campaign_id: 'campaign-active',
  release_id: 'zone-lite-2.7.5',
  release_state: 'HIL_ONLY',
  zone_id: 'ZONE-MULTAN-02',
  zone_name: 'Multan 02',
  version: '2.7.5',
  status: 'ACTIVE',
  eligible: 1,
  legacy_skipped: 0,
  counts: { PENDING: 1 },
  pause_reason: null,
  actor: 'StateHealthAdmin',
  reason: 'One-click firmware update',
  deployments: [],
  waiting: null,
  created_at: '2026-10-23T08:00:00Z',
  updated_at: '2026-10-23T08:00:00Z',
  archived_at: null,
  archived_by: null,
  ...changes,
})

const rows = [
  campaign({
    waiting: {
      connector_id: 'connector-multan-02', display_name: 'Multan 02', code: 'NATIONWIDE_TARGET_OFFLINE',
      message: 'Waiting for the device to be online as an approved fleet target.',
    },
  }),
  campaign({ campaign_id: 'campaign-cancelled', zone_id: 'ZONE-SWAT', zone_name: 'Swat', status: 'CANCELLED', counts: { CANCELLED: 1 } }),
]

const server = () =>
  vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    const method = init?.method ?? 'GET'
    if (path.startsWith('/api/v1/firmware/releases')) {
      return response({ enabled: false, hil_enabled: true, rows: [], next_cursor: null, filtered_total: 0,
        totals: { all: 0, available: 0, hil_only: 0, revoked: 0 } })
    }
    if (method === 'GET' && path.startsWith('/api/v1/firmware/campaigns')) {
      return response({ enabled: false, hil_enabled: true, rows, next_cursor: null, filtered_total: rows.length,
        totals: { campaigns: { all: 2, active: 1, cancelled: 1 }, deployments: { all: 2 }, archived_campaigns: 3 } })
    }
    if (path.endsWith('/campaign-active/cancel')) return response({ campaign_id: 'campaign-active', status: 'CANCELLED' })
    if (path.endsWith('/campaign-cancelled/archive')) {
      return response({ campaign_id: 'campaign-cancelled', status: 'CANCELLED', archived_at: '2026-10-23T09:00:00Z',
        archived_by: 'StateHealthAdmin' })
    }
    if (path.endsWith('/campaigns/archive-cancelled')) return response({ archived: 4 })
    if (path.endsWith('/campaigns/clear-history')) {
      return response({ completed: 1, cancelled: 2, deleted: 3, archived: 5, more: false, kept_running: [
        { campaign_id: 'campaign-booting', zone_id: 'ZONE-QUETTA', connector: 'Quetta 01', connector_id: 'c-q', state: 'BOOTED_PENDING' },
      ] })
    }
    return response({ detail: `Unexpected ${method} ${path}` }, 404)
  })

const toast = { toast: null, notice: vi.fn(), error: vi.fn(), dismiss: vi.fn() }

const view = () =>
  render(
    <FirmwareView devices={[] as Device[]} revision={0} toast={toast} section="campaigns" onSection={vi.fn()} />,
  )

const posts = (fetchMock: ReturnType<typeof server>) =>
  (fetchMock.mock.calls as unknown as Array<[string, RequestInit?]>).filter(([, init]) => init?.method === 'POST')

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('one-click firmware campaign operations', () => {
  it('shows a campaign waiting for its offline device', async () => {
    vi.stubGlobal('fetch', server())
    view()
    expect(await screen.findByText('Waiting for the device to come online')).toBeTruthy()
    expect(screen.getByText(/Show archived \(3\)/)).toBeTruthy()
  })

  it('cancels with one click and no password, reason or dialog', async () => {
    const fetchMock = server()
    vi.stubGlobal('fetch', fetchMock)
    view()
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel campaign for Multan 02' }))
    await waitFor(() => expect(toast.notice).toHaveBeenCalledWith('Multan 02: campaign cancelled.'))
    const [[path, init]] = posts(fetchMock)
    expect(path).toBe('/api/v1/firmware/campaigns/campaign-active/cancel')
    expect(JSON.parse(String(init?.body))).toEqual({})
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByLabelText('Administrator password')).toBeNull()
    // Only valid controls are offered: no resume on an active campaign, no cancel on a cancelled one.
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel campaign for Swat' })).toBeNull()
  })

  it('archives one cancelled campaign, then all of them, with one click each', async () => {
    const fetchMock = server()
    vi.stubGlobal('fetch', fetchMock)
    view()
    fireEvent.click(await screen.findByRole('button', { name: 'Archive campaign for Swat' }))
    await waitFor(() => expect(toast.notice).toHaveBeenCalledWith('Swat: campaign archived.'))
    expect(screen.queryByRole('button', { name: 'Archive campaign for Multan 02' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Archive all cancelled' }))
    await waitFor(() => expect(toast.notice).toHaveBeenCalledWith('4 cancelled or completed campaigns archived.'))
    expect(posts(fetchMock).map(([path]) => path)).toEqual([
      '/api/v1/firmware/campaigns/campaign-cancelled/archive',
      '/api/v1/firmware/campaigns/archive-cancelled',
    ])
  })

  it('clears firmware history with one click and shows what was kept running', async () => {
    const fetchMock = server()
    vi.stubGlobal('fetch', fetchMock)
    view()
    fireEvent.click(await screen.findByRole('button', { name: 'Clear firmware history' }))
    expect(await screen.findByText('Firmware history cleared')).toBeTruthy()
    expect(screen.getByText(/1 completed · 2 cancelled · 3 removed · 5 archived/)).toBeTruthy()
    expect(screen.getByText(/ZONE-QUETTA · Quetta 01 \(booted pending\)/)).toBeTruthy()
    expect(posts(fetchMock).map(([path]) => path)).toEqual(['/api/v1/firmware/campaigns/clear-history'])
  })

  it('lists archived campaigns only when asked', async () => {
    const fetchMock = server()
    vi.stubGlobal('fetch', fetchMock)
    view()
    await screen.findByText('Waiting for the device to come online')
    const lists = () => (fetchMock.mock.calls as unknown as Array<[string]>)
      .map(([path]) => path)
      .filter((path) => path.startsWith('/api/v1/firmware/campaigns?'))
    expect(lists().every((path) => !path.includes('include_archived'))).toBe(true)
    fireEvent.click(screen.getByRole('checkbox', { name: /Show archived/ }))
    await waitFor(() => expect(lists().some((path) => path.includes('include_archived=true'))).toBe(true))
  })
})

const signedRelease = (changes: Partial<FirmwareRelease>): FirmwareRelease => ({
  release_id: 'zone-lite-2.7.8',
  version: '2.7.8',
  firmware_family: 'zkt',
  git_sha: 'a'.repeat(40),
  image_sha256: 'b'.repeat(64),
  application_sha256: 'c'.repeat(64),
  image_size: 1_572_864,
  state: 'AVAILABLE',
  partition_layout: 'zone-lite-ota-v1',
  signing_key_id: 'production-key',
  published_at: '2026-10-28T08:00:00Z',
  revoked_at: null,
  revoked_by: null,
  hil_target_mac: null,
  ...changes,
})

const newest = signedRelease({ devices_running: 3, current: true })
const superseded = signedRelease({
  release_id: 'zone-lite-2.6.0', version: '2.6.0', image_sha256: 'd'.repeat(64), devices_running: 0, current: false,
})
// An older ADD omits the new fields: such a release must stay listed.
const unannotated = signedRelease({ release_id: 'zone-lite-2.5.2', version: '2.5.2', image_sha256: 'e'.repeat(64) })

const releaseServer = () =>
  vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path.startsWith('/api/v1/firmware/releases')) {
      const rows = new URLSearchParams(path.split('?')[1]).get('scope') === 'current'
        ? [newest]
        : [newest, superseded, unannotated]
      return response({ enabled: true, hil_enabled: false, rows, next_cursor: null, filtered_total: rows.length,
        totals: { all: 3, available: 3, hil_only: 0, revoked: 0, current: 1, hidden: 2 } })
    }
    if (path.startsWith('/api/v1/firmware/campaigns')) {
      return response({ enabled: true, hil_enabled: false, rows: [], next_cursor: null, filtered_total: 0,
        totals: { campaigns: { all: 0 }, deployments: { all: 0 } } })
    }
    return response({ detail: `Unexpected ${path}` }, 404)
  })

const releasesView = () =>
  render(
    <FirmwareView devices={[] as Device[]} revision={0} toast={toast} section="releases" onSection={vi.fn()} />,
  )

describe('signed release inventory hides superseded releases without revoking them', () => {
  it('lists current releases with their device counts and shows older ones only on request', async () => {
    const fetchMock = releaseServer()
    vi.stubGlobal('fetch', fetchMock)
    releasesView()
    expect(await screen.findByText('2 older releases hidden')).toBeTruthy()
    expect(screen.getByText('3 devices')).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'ZKT Zone Lite 2.6.0' })).toBeNull()
    const inventory = () => (fetchMock.mock.calls as unknown as Array<[string]>)
      .map(([path]) => path)
      .filter((path) => path.startsWith('/api/v1/firmware/releases?') && !path.includes('limit=200'))
    expect(inventory().length).toBeGreaterThan(0)
    expect(inventory().every((path) => path.includes('scope=current'))).toBe(true)

    fireEvent.click(screen.getByRole('checkbox', { name: 'Show older releases' }))
    expect(await screen.findByRole('heading', { name: 'ZKT Zone Lite 2.6.0' })).toBeTruthy()
    expect(inventory().at(-1)).not.toContain('scope=')
    expect(screen.queryByText('2 older releases hidden')).toBeNull()
    expect(screen.getByText('0 devices')).toBeTruthy()
    // Hiding is a view: nothing is revoked or otherwise changed.
    expect(posts(fetchMock)).toEqual([])
  })

  it('offers only current releases in the update dialog unless older ones are shown', async () => {
    vi.stubGlobal('fetch', releaseServer())
    releasesView()
    const update = (await screen.findByRole('button', { name: 'Update firmware' })) as HTMLButtonElement
    await waitFor(() => expect(update.disabled).toBe(false))
    const offered = () =>
      Array.from((screen.getByLabelText('Release') as HTMLSelectElement).options).map((option) => option.value)

    fireEvent.click(update)
    expect(offered()).toEqual(['zone-lite-2.7.8', 'zone-lite-2.5.2'])
    fireEvent.click(screen.getByRole('button', { name: 'Close dialog' }))

    fireEvent.click(screen.getByRole('checkbox', { name: 'Show older releases' }))
    fireEvent.click(update)
    expect(offered()).toEqual(['zone-lite-2.7.8', 'zone-lite-2.6.0', 'zone-lite-2.5.2'])
  })
})
