import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Device, FirmwareCampaign } from '../types'
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
      connector_id: 'connector-multan-02', display_name: 'Multan 02', code: 'NATIONWIDE_TWO_UPGRADE_RESERVATIONS',
      message: 'Waiting for the nationwide ZKT upgrade slot and exact target checks.',
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
  it('shows a campaign waiting for a fleet slot', async () => {
    vi.stubGlobal('fetch', server())
    view()
    expect(await screen.findByText('Waiting for a fleet slot')).toBeTruthy()
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
    await screen.findByText('Waiting for a fleet slot')
    const lists = () => (fetchMock.mock.calls as unknown as Array<[string]>)
      .map(([path]) => path)
      .filter((path) => path.startsWith('/api/v1/firmware/campaigns?'))
    expect(lists().every((path) => !path.includes('include_archived'))).toBe(true)
    fireEvent.click(screen.getByRole('checkbox', { name: /Show archived/ }))
    await waitFor(() => expect(lists().some((path) => path.includes('include_archived=true'))).toBe(true))
  })
})
