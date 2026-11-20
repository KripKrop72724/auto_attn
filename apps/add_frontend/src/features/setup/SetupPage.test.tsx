import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { expectPlainText } from '../../words/testing'
import SetupPage from './SetupPage'

const WINDOWS = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36'
const PHONE = 'Mozilla/5.0 (Linux; Android 15; Pixel 9)'

const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const disabled = () => response({ code: 'INSTALLER_DISABLED', message: 'Off.', detail: { code: 'INSTALLER_DISABLED', message: 'Off.' } }, 409)
const apps = {
  latest: {
    version: '1.1.0', git_sha: 'c'.repeat(40), published_at: '2026-11-04T09:30:00Z',
    files: [{ platform: 'windows', arch: 'x64', kind: 'setup', filename: 'StateLife-Device-Installer-Setup-1.1.0.exe', size: 86_123_456, sha256: 'a'.repeat(64), download_path: '/api/v1/installer/apps/1.1.0/setup.exe' }],
  },
  min_app_version: '1.0.0',
}
const session = {
  session_id: 'session-1', operator: 'StateHealthAdmin', companion_id: null, hardware_mac: 'ac:27:6e:a3:45:44', hardware_classification: 'BLANK_NEW',
  hardware_evidence: {}, mode: null, bundle: { version: '2.8.2' }, zone_id: 'LF-ZONE-GUJRAT', zone_name: 'Gujrat', device_id: 'LF-ZONE-GUJRAT-01',
  preferred_ip: null, zkt_port: 4370, state: 'VERIFIED_ONLINE', progress: 100, cancellable: false, result: { connector_id: 'connector-gujrat' },
  expires_at: '2026-11-04T10:30:00Z', created_at: '2026-11-04T09:30:00Z', updated_at: '2026-11-04T09:50:00Z', events: [],
}

type Plan = { installer?: () => Response; capabilities?: () => Response; sessions?: unknown[] }
const server = ({ installer = disabled, capabilities = () => response({ enabled: false, can_start: false, latest_bundle: null }), sessions = [] }: Plan = {}) =>
  vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path.startsWith('/api/v1/installer/apps')) return installer()
    if (path.startsWith('/api/v1/provisioning/capabilities')) return capabilities()
    if (path.startsWith('/api/v1/provisioning/sessions/session-1')) return response(session)
    if (path.startsWith('/api/v1/provisioning/sessions')) return response({ rows: sessions, next_cursor: null, filtered_total: sessions.length })
    return response({ rows: [] })
  })

const toast = { toast: null, notice: vi.fn(), error: vi.fn(), dismiss: vi.fn() }
const renderAs = (userAgent: string, fetchMock: ReturnType<typeof server>, onOpenDevice = vi.fn()) => {
  vi.stubGlobal('navigator', { ...navigator, userAgent, maxTouchPoints: 0, clipboard: { writeText: vi.fn(async () => undefined) } })
  vi.stubGlobal('fetch', fetchMock)
  return render(
    <MemoryRouter initialEntries={['/setup']}>
      <SetupPage revision={0} toast={toast} username="StateHealthAdmin" onOpenDevice={onOpenDevice} />
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('Set up a device', () => {
  it('shows a skeleton, never "switched off", before the server answers', () => {
    const pending = vi.fn(() => new Promise<Response>(() => undefined))
    renderAs(WINDOWS, pending as unknown as ReturnType<typeof server>)
    expect(screen.getByRole('heading', { name: 'Set up a device', level: 1 })).toBeTruthy()
    expect(screen.queryByText(/switched off/i)).toBeNull()
    expect(screen.getAllByText(/Loading Set up a device/).length).toBeGreaterThan(0)
  })

  it('shows one switched-off card and nothing else while setup is off', async () => {
    const { container } = renderAs(WINDOWS, server())
    expect(await screen.findByText('Switched off for now')).toBeTruthy()
    expect(screen.getByText(/For now IT prepares new link boxes/)).toBeTruthy()
    expect(screen.getByText('Need a new device? Ask IT.')).toBeTruthy()
    expect(screen.queryByText(/Physical provisioning is disabled/)).toBeNull()
    expect(screen.queryByLabelText('Provisioning readiness')).toBeNull()
    expect(screen.queryByRole('link', { name: /Download for/ })).toBeNull()
    // No past setups: the section is hidden.
    await waitFor(() => expect(screen.queryByText('Past setups')).toBeNull())
    expectPlainText(container)
  })

  it('leads with the State Life Device Installer when it is on, without the browser setup', async () => {
    const { container } = renderAs(WINDOWS, server({ installer: () => response(apps) }))
    expect(await screen.findByRole('link', { name: 'Download for Windows' })).toBeTruthy()
    expect(screen.getByText("Setting up from this browser isn't available yet. Use the State Life Device Installer above.")).toBeTruthy()
    expect(screen.getByText('a'.repeat(64)).closest('[data-technical]')).toBeTruthy()
    expectPlainText(container, { skip: '.installer-app-first-run' })
  })

  it('hides the downloads and the browser setup on a phone', async () => {
    renderAs(PHONE, server({ installer: () => response(apps), capabilities: () => response({ enabled: true, can_start: true, latest_bundle: null }) }))
    expect(await screen.findByText(/to download the app/)).toBeTruthy()
    expect(screen.getByText(/Here you can still see past setups/)).toBeTruthy()
    expect(screen.queryByRole('link', { name: /Download for/ })).toBeNull()
    expect(screen.queryByLabelText('Provisioning progress')).toBeNull()
  })

  it('says when the page could not be loaded and tries again', async () => {
    const fetchMock = server({ installer: () => response({ detail: 'boom' }, 500) })
    renderAs(WINDOWS, fetchMock)
    const retry = await screen.findByRole('button', { name: 'Try again' })
    expect(screen.getByText("Couldn't load this page.")).toBeTruthy()
    fetchMock.mockImplementation(server())
    fireEvent.click(retry)
    expect(await screen.findByText('Switched off for now')).toBeTruthy()
  })
})

describe('Past setups', () => {
  it('lists setups by place with the Life tag, searches, and opens a summary', async () => {
    const onOpenDevice = vi.fn()
    const fetchMock = server({ sessions: [session] })
    renderAs(WINDOWS, fetchMock, onOpenDevice)
    expect(await screen.findByRole('heading', { name: 'Past setups' })).toBeTruthy()
    expect(await screen.findByText('1 setup. Passwords are never saved.')).toBeTruthy()
    const row = screen.getByRole('button', { name: /Gujrat · Device 1/ })
    expect(within(row).getByText('Life')).toBeTruthy()
    expect(within(row).getByText(/· You$/)).toBeTruthy()
    expect(within(row).queryByText('ac:27:6e:a3:45:44')).toBeNull()
    fireEvent.click(row)
    const sheet = await screen.findByRole('dialog', { name: 'Device is working' })
    expect(within(sheet).getByText('The link box is online, connected to its machine and sending attendance.')).toBeTruthy()
    expect(within(sheet).getByText('ac:27:6e:a3:45:44').closest('[data-technical]')).toBeTruthy()
    fireEvent.click(within(sheet).getByRole('button', { name: 'Open device' }))
    expect(onOpenDevice).toHaveBeenCalledWith('connector-gujrat')
    // Mine is the default; Everyone drops mine_only.
    const lists = () => (fetchMock.mock.calls as unknown as Array<[string]>).map(([path]) => path).filter((path) => path.startsWith('/api/v1/provisioning/sessions?'))
    expect(lists()[0]).toContain('mine_only=true')
    fireEvent.click(screen.getByRole('radio', { name: 'Everyone' }))
    await waitFor(() => expect(lists().at(-1)).not.toContain('mine_only'))
  })
})
