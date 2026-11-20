import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Device, FirmwareCampaign, FirmwareRelease, UpdatesSection } from '../types'
import { expectPlainText } from '../words/testing'
import { FirmwareView } from './Firmware'
import { visibleInterval } from './updates/model'

const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const MULTAN = 'beb5f8eb-0000-4000-8000-000000000002'
const makeDevice = (changes: Partial<Device>): Device => ({
  connector_id: MULTAN, hardware_id: 'ac:27:6e:a3:de:e8', zone_id: 'ZONE-MULTAN-02', zone_name: 'ZONE-MULTAN-02', device_id: '2',
  display_name: 'ZONE-MULTAN-02', friendly_name: 'Multan · Device 2', division: 'health', state: 'ONLINE', connected: true,
  firmware_version: 'zone-lite-2.7.6', firmware_family: 'zkt', ota_capable: true, ota_state: 'OTA_READY',
  ...changes,
} as Device)
const devices: Device[] = [
  makeDevice({}),
  makeDevice({ connector_id: 'c-swat', zone_id: 'ZONE-SWAT', display_name: 'ZONE-SWAT', friendly_name: 'Swat', firmware_version: 'zone-lite-2.7.8' }),
  makeDevice({ connector_id: 'c-bld9', zone_id: 'LF-ZONE-BLD9-02', display_name: 'LF-ZONE-BLD9-02', friendly_name: 'Building 9 · Device 2', division: 'life', firmware_version: 'zone-lite-2.7.8' }),
  makeDevice({ connector_id: 'c-spare', zone_id: 'ZONE-SPARE', display_name: 'ZONE-SPARE', friendly_name: 'Spare box', is_spare: true }),
]

const campaign = (changes: Partial<FirmwareCampaign>): FirmwareCampaign => ({
  campaign_id: 'campaign-active',
  release_id: 'zone-lite-2.7.8',
  release_state: 'AVAILABLE',
  zone_id: 'ZONE-MULTAN-02',
  zone_name: 'ZONE-MULTAN-02',
  version: '2.7.8',
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

const active = campaign({
  waiting: { connector_id: MULTAN, display_name: 'Multan 02', code: 'NATIONWIDE_TARGET_OFFLINE', message: 'Waiting for the device to be online as an approved fleet target.' },
})
const cancelled = campaign({ campaign_id: 'campaign-cancelled', zone_id: 'ZONE-SWAT', zone_name: 'Swat', status: 'CANCELLED', counts: { CANCELLED: 1 } })

const release = (changes: Partial<FirmwareRelease>): FirmwareRelease => ({
  release_id: 'zone-lite-2.7.8', version: '2.7.8', firmware_family: 'zkt', git_sha: 'a'.repeat(40), image_sha256: 'b'.repeat(64),
  application_sha256: 'c'.repeat(64), image_size: 1_572_864, state: 'AVAILABLE', partition_layout: 'zone-lite-ota-v1',
  signing_key_id: 'production-key', published_at: '2026-10-28T08:00:00Z', revoked_at: null, revoked_by: null, hil_target_mac: null,
  current: true, ...changes,
})
const newest = release({})
const older = release({ release_id: 'zone-lite-2.6.0', version: '2.6.0', image_sha256: 'd'.repeat(64), current: false })

type Options = { rows?: FirmwareCampaign[]; live?: FirmwareCampaign[]; enabled?: boolean; releases?: FirmwareRelease[] }
const server = ({ rows = [active, cancelled], live = [active], enabled = true, releases = [newest, older] }: Options = {}) =>
  vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    const method = init?.method ?? 'GET'
    const query = new URLSearchParams(path.split('?')[1] || '')
    if (path.startsWith('/api/v1/firmware/releases')) {
      const shown = query.get('scope') === 'current' ? releases.filter((row) => row.current !== false) : releases
      return response({ enabled, hil_enabled: false, rows: shown, next_cursor: null, filtered_total: shown.length,
        totals: { all: releases.length, available: releases.length, hil_only: 0, revoked: 0 } })
    }
    if (method === 'GET' && path.startsWith('/api/v1/firmware/campaigns?')) {
      // The live query asks for one status at a time with the devices.
      if (query.get('status') && !query.get('view')) {
        const shown = live.filter((row) => row.status === query.get('status'))
        return response({ enabled, hil_enabled: false, rows: shown, next_cursor: null, filtered_total: shown.length, totals: { campaigns: { all: 0 }, deployments: { all: 0 } } })
      }
      return response({ enabled, hil_enabled: false, rows, next_cursor: null, filtered_total: rows.length,
        totals: { campaigns: { all: 2, active: 1, cancelled: 1 }, deployments: { all: 2 }, archived_campaigns: 3 } })
    }
    if (method === 'GET' && path.startsWith('/api/v1/firmware/campaigns/')) return response(rows.find((row) => path.endsWith(row.campaign_id)) ?? rows[0])
    if (path.endsWith('/campaign-active/cancel')) return response({ campaign_id: 'campaign-active', status: 'CANCELLED' })
    if (path.endsWith('/campaign-cancelled/archive')) return response({ campaign_id: 'campaign-cancelled', status: 'CANCELLED', archived_at: '2026-10-23T09:00:00Z' })
    if (path.endsWith('/campaigns/archive-cancelled')) return response({ archived: 4 })
    if (path.endsWith('/campaigns/clear-history')) {
      return response({ completed: 1, cancelled: 2, deleted: 3, archived: 5, more: true, kept_running: [
        { campaign_id: 'campaign-booting', zone_id: 'ZONE-QUETTA', connector: 'Quetta 01', connector_id: 'c-q', state: 'BOOTED_PENDING' },
      ] })
    }
    if (path.includes('/revoke')) return response({ release_id: 'zone-lite-2.7.8', state: 'REVOKED' })
    return response({ detail: `Unexpected ${method} ${path}` }, 404)
  })

const toast = { toast: null, notice: vi.fn(), error: vi.fn(), dismiss: vi.fn() }

function Page({ devicesLoaded = true, list = devices }: { devicesLoaded?: boolean; list?: Device[] }) {
  const location = useLocation()
  const tab = (new URLSearchParams(location.search).get('tab') || 'overview') as UpdatesSection
  return <FirmwareView devices={list} devicesLoaded={devicesLoaded} revision={0} toast={toast} section={tab} />
}
const view = (path = '/updates?tab=updates', props: { devicesLoaded?: boolean; list?: Device[] } = {}) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes><Route path="/updates" element={<Page {...props} />} /></Routes>
    </MemoryRouter>,
  )

const posts = (fetchMock: ReturnType<typeof server>) =>
  (fetchMock.mock.calls as unknown as Array<[string, RequestInit?]>).filter(([, init]) => init?.method === 'POST')
const gets = (fetchMock: ReturnType<typeof server>) =>
  (fetchMock.mock.calls as unknown as Array<[string, RequestInit?]>).filter(([, init]) => (init?.method ?? 'GET') === 'GET').map(([path]) => path)

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
  vi.useRealTimers()
  localStorage.clear()
})

describe('Software updates status line (one rule, no fake zeros)', () => {
  it('shows a skeleton and no "0/0" until the device list arrives', async () => {
    vi.stubGlobal('fetch', server())
    const { container } = view('/updates', { devicesLoaded: false, list: [] })
    expect(await screen.findByRole('heading', { name: 'Software updates', level: 1 })).toBeTruthy()
    expect(container.textContent).not.toMatch(/0\/0|0 of 0/)
    expect(screen.getAllByText(/Loading the software status/).length).toBeGreaterThan(0)
  })

  it('counts devices with softwareStatus and leaves spares out', async () => {
    vi.stubGlobal('fetch', server())
    view('/updates')
    const status = await screen.findByRole('region', { name: 'Software status' })
    await waitFor(() => expect(status.textContent).toMatch(/2 of 3 devices are up to date/))
    expect(status.textContent).toMatch(/1 spare device is not counted/)
    expect(screen.queryByText(/OTA-ready/)).toBeNull()
  })

  it('says when updates for all devices are switched off', async () => {
    vi.stubGlobal('fetch', server({ enabled: false }))
    view('/updates')
    expect(await screen.findByText('Updates for all devices are switched off for now. Ask IT to switch them on.')).toBeTruthy()
    expect(await screen.findByText('No version is ready to install.')).toBeTruthy()
  })

  it('keeps the page in plain words outside Technical details', async () => {
    vi.stubGlobal('fetch', server())
    const { container } = view('/updates')
    await screen.findByText(/2 of 3 devices are up to date/)
    await screen.findAllByText('Multan · Device 2')
    expectPlainText(container)
  })

  it('loads running updates on their own, whatever the Updates filter says', async () => {
    const fetchMock = server()
    vi.stubGlobal('fetch', fetchMock)
    view('/updates?tab=updates&show=stopped')
    await screen.findByText(/Swat/)
    const live = gets(fetchMock).filter((path) => /campaigns\?.*status=(ACTIVE|PAUSED)/.test(path) && !path.includes('view='))
    expect(live.some((path) => path.includes('status=ACTIVE'))).toBe(true)
    expect(live.some((path) => path.includes('status=PAUSED'))).toBe(true)
    await waitFor(() => expect(gets(fetchMock).some((path) => path.includes('view=summary') && path.includes('status=CANCELLED'))).toBe(true))
  })
})

describe('Updates tab: one-click actions', () => {
  it('shows an update waiting for its offline device by name', async () => {
    vi.stubGlobal('fetch', server())
    view()
    expect(await screen.findByText('Waiting for Multan · Device 2 to come online.')).toBeTruthy()
    expect(screen.getAllByText('Waiting for device').length).toBeGreaterThan(0)
  })

  it('stops with one click and no password, reason or dialog', async () => {
    const fetchMock = server()
    vi.stubGlobal('fetch', fetchMock)
    view()
    fireEvent.click(await screen.findByRole('button', { name: 'Stop update at Multan · Device 2' }))
    await waitFor(() => expect(toast.notice).toHaveBeenCalledWith('Update stopped at Multan · Device 2. Start a new update to try again.'))
    const [[path, init]] = posts(fetchMock)
    expect(path).toBe('/api/v1/firmware/campaigns/campaign-active/cancel')
    expect(JSON.parse(String(init?.body))).toEqual({})
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByLabelText(/password/i)).toBeNull()
    // Only valid controls: no Continue on a running update, no Stop on a stopped one.
    expect(screen.queryByRole('button', { name: 'Continue update at Multan · Device 2' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop update at Swat' })).toBeNull()
  })

  it('hides one finished update, then all of them, with one click each', async () => {
    const fetchMock = server()
    vi.stubGlobal('fetch', fetchMock)
    view()
    fireEvent.click(await screen.findByRole('button', { name: 'Hide update at Swat' }))
    await waitFor(() => expect(toast.notice).toHaveBeenCalledWith('Swat: hidden from the list.'))
    expect(screen.queryByRole('button', { name: 'Hide update at Multan · Device 2' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'More update actions' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Hide finished updates' }))
    await waitFor(() => expect(toast.notice).toHaveBeenCalledWith('4 finished updates hidden.'))
    expect(posts(fetchMock).map(([path]) => path)).toEqual([
      '/api/v1/firmware/campaigns/campaign-cancelled/archive',
      '/api/v1/firmware/campaigns/archive-cancelled',
    ])
  })

  it('clears history with one click, says what it did and offers to clear the rest', async () => {
    const fetchMock = server()
    vi.stubGlobal('fetch', fetchMock)
    view()
    expect(await screen.findByText('Stops updates that have not started installing, and clears old ones. Records are kept.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Clear history' }))
    const banner = await screen.findByText('Old updates cleared')
    const box = banner.closest('[role="status"]') as HTMLElement
    expect(within(box).getByText('2 stopped · 8 cleared from the list')).toBeTruthy()
    expect(within(box).getByText(/Left running \(still installing\): Quetta — restarting into the new version/)).toBeTruthy()
    expect(within(box).getByRole('button', { name: 'Clear the rest' })).toBeTruthy()
    expect(posts(fetchMock).map(([path]) => path)).toEqual(['/api/v1/firmware/campaigns/clear-history'])
  })

  it('turns the stored pause reason into the device and a plain sentence', async () => {
    const paused = campaign({ campaign_id: 'campaign-paused', status: 'PAUSED', counts: { ROLLED_BACK: 1 }, pause_reason: `${MULTAN}: ROLLED_BACK (TARGET_BOOT_INTERRUPTED)` })
    vi.stubGlobal('fetch', server({ rows: [paused], live: [paused] }))
    view()
    expect(await screen.findByText(
      'Paused because Multan · Device 2 could not update: The link box restarted before it finished installing and went back to the old version. Attendance is safe.',
    )).toBeTruthy()
    expect(screen.getAllByText('Waiting for you').length).toBeGreaterThan(0)
    // The raw text is kept, under Technical details only.
    const raw = screen.getByText(`${MULTAN}: ROLLED_BACK (TARGET_BOOT_INTERRUPTED)`)
    expect(raw.closest('[data-technical]')).toBeTruthy()
  })

  it('lists hidden updates only when asked', async () => {
    const fetchMock = server()
    vi.stubGlobal('fetch', fetchMock)
    view()
    await screen.findByText('Waiting for Multan · Device 2 to come online.')
    const lists = () => gets(fetchMock).filter((path) => path.includes('view=summary'))
    expect(lists().every((path) => !path.includes('include_archived'))).toBe(true)
    view('/updates?tab=updates&hidden=1')
    await waitFor(() => expect(lists().some((path) => path.includes('include_archived=true'))).toBe(true))
  })
})

describe('live movement', () => {
  it('polls only while active and only when the tab is visible', () => {
    vi.useFakeTimers()
    const run = vi.fn()
    const stop = visibleInterval(true, 15_000, run)
    vi.advanceTimersByTime(15_000)
    expect(run).toHaveBeenCalledTimes(1)
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    vi.advanceTimersByTime(15_000)
    expect(run).toHaveBeenCalledTimes(1)
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
    stop()
    vi.advanceTimersByTime(30_000)
    expect(run).toHaveBeenCalledTimes(1)
    const idle = vi.fn()
    visibleInterval(false, 15_000, idle)()
    vi.advanceTimersByTime(30_000)
    expect(idle).not.toHaveBeenCalled()
  })
})

describe('Versions tab', () => {
  it('shows versions in use first, older ones on request, with where they run', async () => {
    const fetchMock = server()
    vi.stubGlobal('fetch', fetchMock)
    view('/updates?tab=versions')
    expect(await screen.findByRole('heading', { name: 'Software versions' })).toBeTruthy()
    expect(await screen.findByText(/On 2 devices \(1 Life · 1 Health\)/)).toBeTruthy()
    expect(screen.queryByText('2.6.0')).toBeNull()
    const inventory = () => gets(fetchMock).filter((path) => path.startsWith('/api/v1/firmware/releases?') && !path.includes('limit=200'))
    expect(inventory().every((path) => path.includes('scope=current'))).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: /All versions/ }))
    expect(await screen.findByText('2.6.0')).toBeTruthy()
    expect(inventory().at(-1)).not.toContain('scope=')
    // Fingerprints and the signing key sit in Technical details.
    expect(screen.getAllByText('b'.repeat(64))[0].closest('[data-technical]')).toBeTruthy()
    expect(posts(fetchMock)).toEqual([])
  })

  it('withdraws from the menu with the note and password; Keep this version clears both', async () => {
    const fetchMock = server()
    vi.stubGlobal('fetch', fetchMock)
    view('/updates?tab=versions')
    fireEvent.click(await screen.findByRole('button', { name: 'More actions for version 2.7.8' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Withdraw version…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Withdraw version 2.7.8' })
    fireEvent.change(within(dialog).getByLabelText(/Why are you withdrawing it/), { target: { value: 'Bad build seen at Swat' } })
    fireEvent.change(within(dialog).getByLabelText('Your password'), { target: { value: 'secret' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Keep this version' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    fireEvent.click(screen.getByRole('button', { name: 'More actions for version 2.7.8' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Withdraw version…' }))
    const again = await screen.findByRole('dialog', { name: 'Withdraw version 2.7.8' })
    expect((within(again).getByLabelText(/Why are you withdrawing it/) as HTMLTextAreaElement).value).toBe('')
    expect((within(again).getByLabelText('Your password') as HTMLInputElement).value).toBe('')
    const submit = within(again).getByRole('button', { name: 'Withdraw version' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    fireEvent.change(within(again).getByLabelText(/Why are you withdrawing it/), { target: { value: 'Bad build seen at Swat' } })
    fireEvent.change(within(again).getByLabelText('Your password'), { target: { value: 'secret' } })
    fireEvent.click(submit)
    await waitFor(() => expect(posts(fetchMock).length).toBe(1))
    const [[path, init]] = posts(fetchMock)
    expect(path).toBe('/api/v1/firmware/releases/zone-lite-2.7.8/revoke')
    expect(JSON.parse(String(init?.body))).toEqual({ reason: 'Bad build seen at Swat', password: 'secret' })
  })
})

describe('Update software sheet', () => {
  it('offers only versions in use and devices grouped Life / Health', async () => {
    vi.stubGlobal('fetch', server({ live: [] }))
    view('/updates')
    const update = (await screen.findByRole('button', { name: 'Update software' })) as HTMLButtonElement
    await waitFor(() => expect(update.disabled).toBe(false))
    fireEvent.click(update)
    const sheet = await screen.findByRole('dialog', { name: 'Update software' })
    expect(within(sheet).getAllByRole('radio').length).toBe(1)
    expect(within(sheet).getByRole('radio', { name: /2\.7\.8/ })).toBeTruthy()
    // Swat and Building 9 are already on 2.7.8: hidden until asked.
    expect(within(sheet).queryByText('Swat')).toBeNull()
    expect(within(sheet).getByText('Now on 2.7.6')).toBeTruthy()
    fireEvent.click(within(sheet).getByRole('checkbox', { name: /Choose all Health/ }))
    expect(within(sheet).getByRole('button', { name: 'Update 1 device (0 Life · 1 Health)' })).toBeTruthy()
    expect(within(sheet).queryByLabelText(/password/i)).toBeNull()
  })
})
