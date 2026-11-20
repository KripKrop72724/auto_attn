import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App, {
  buildMachinePreview,
  bulkDeletionConfirmation,
  CommandProgress,
  confirmationMatches,
  formatAlertDiagnostics,
  identityConflictText,
  metricsTime,
  pktInputToUtc,
  pktTodayBounds,
  StatusBadge,
  validateUserDraft,
} from './App'
import { api } from './api'
import type { Device, DeviceUser, Overview } from './types'
import {
  conflictedUser, deliveryAlert, device, fetchStub, fullCnic, maskedCnic, response, selectUserTerminal, useDashboardStubs, user,
  CapturingEventSource,
} from './test/renderApp'
import { queryClient } from './data'

describe('State Life ADD interface', () => {
  useDashboardStubs(() => Promise.all([import('./features/Fleet'), import('./features/Login'), import('./features/DeviceDrawer'), import('./features/FleetMap'), import('./features/Attendance')]))

  it('builds an exact device-scoped bulk deletion confirmation', () => {
    expect(bulkDeletionConfirmation(12, 'ZONE-SWAT-01')).toBe(
      'DELETE 12 USERS FROM ZONE-SWAT-01',
    )
  })

  it('uses secure automatic onboarding language and exposes no registration control', async () => {
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Fleet', level: 1 })).toBeTruthy()
    expect(screen.getByAltText('State Life Insurance Corporation')).toBeTruthy()
    expect(screen.queryByText(/register connector/i)).toBeNull()
    expect(screen.getByText(/secure auto-onboarding enabled/i)).toBeTruthy()
    expect(screen.getByText('ORDS delivery queue')).toBeTruthy()
    expect(await screen.findByText('12')).toBeTruthy()
    expect(await screen.findByText(/3 retrying · 2 identity blocked · 1 quarantined/i)).toBeTruthy()
  })

  it('shows the unknown-identity automatic release on Fleet and opens it on Attendance', async () => {
    const base = fetchStub()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/v2/unknown-identity-auto-release') {
        return response({
          allowed: true, enabled: false, active: false, changed_by: 'StateHealthAdmin',
          changed_at: '2026-11-05T12:00:05Z', reason: 'Oracle maintenance', drain_active: false,
          backlog: { count: 108000, counted_at: '2026-11-05T14:00:00Z', remaining_estimate: 108000 },
          progress: { ready: 0, waiting_oracle: 0, in_flight: 0, confirmed_total: 0, confirmed_last_hour: 0, skipped_total: 0, skipped_by_reason: [] },
          rate_per_min: 0, eta_minutes: null, in_flight_limit: 1500, server_time: '2026-11-05T14:00:05Z',
        })
      }
      return base(input, init)
    }))
    render(<App />)
    const tile = await screen.findByRole('button', { name: /Unknown-identity release: OFF\. Turned off by StateHealthAdmin 2 h ago · 108,000 held/ })
    fireEvent.click(tile)
    await waitFor(() => expect(window.location.pathname).toBe('/attendance'))
    // Plain Attendance: the panel shows there without turning on the heavy filter.
    expect(window.location.search).toBe('')
  })

  it('opens on the Pakistan map and preserves the complete list behind an in-memory toggle', async () => {
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    expect(await screen.findByRole('region', { name: 'Pakistan device network map' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Map' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: /Islamabad, 1 device, All online/i })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'List' }))
    expect(await screen.findByRole('button', { name: `Inspect ${device.display_name}` })).toBeTruthy()
    expect(screen.queryByRole('region', { name: 'Pakistan device network map' })).toBeNull()
    // The browser keeps at most one key, add.prefs, and only after a change (D-25).
    expect(Object.keys(window.localStorage).every((key) => key === 'add.prefs')).toBe(true)
    expect(window.sessionStorage.length).toBe(0)
  })

  it('keeps spare devices in a separate inventory view', async () => {
    const spareDevice: Device = {
      ...device,
      connector_id: 'connector-spare',
      hardware_id: 'e0:72:a1:d6:f3:29',
      display_name: 'Islamabad reserve device',
      state: 'OFFLINE',
      connected: false,
      is_spare: true,
    }
    vi.stubGlobal('fetch', fetchStub([user], false, false, false, false, [device, spareDevice]))
    render(<App />)

    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    expect(await screen.findByRole('tab', { name: /active fleet 1/i })).toBeTruthy()
    fireEvent.click(await screen.findByRole('tab', { name: /spares 1/i }))

    expect(await screen.findByRole('heading', { name: /spare device inventory/i })).toBeTruthy()
    expect(screen.getByText(/offline state and alerts do not affect fleet availability/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Inspect Islamabad reserve device' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: `Inspect ${device.display_name}` })).toBeNull()
    expect(screen.getByText('Spare')).toBeTruthy()
  })

  it('counts warnings as online and keeps terminal outages in the attention filter', async () => {
    const minutesAgo = (minutes: number) => new Date(Date.now() - minutes * 60_000 - 5_000).toISOString()
    const warningDevice: Device = {
      ...device,
      connector_id: 'connector-warning',
      hardware_id: 'e0:72:a1:d7:05:c4',
      zone_id: 'ZONE-PESHAWAR-02',
      zone_name: 'Peshawar',
      display_name: 'Peshawar 02',
      state: 'ONLINE_WITH_WARNINGS',
      terminal_link: { state: 'CONNECTED', raw_state: 'ONLINE', since: minutesAgo(90), reason: 'LINK_UP', message: 'The terminal link is up.' },
      health: {
        mode: 'ENFORCED',
        tier: 'ONLINE_WITH_WARNINGS',
        derived_lifecycle: 'ONLINE_WITH_WARNINGS',
        last_error_code: 'ESP_DELIVERY_WORKER_FAULT',
        primary: {
          code: 'ESP_DELIVERY_WORKER_FAULT',
          error_code: 'ESP_DELIVERY_WORKER_FAULT',
          tier: 'WARNING',
          message: 'add_delivery STOPPED',
          since: minutesAgo(37),
        },
        degraded_count: 0,
        warning_count: 1,
      },
    }
    const terminalDownDevice: Device = {
      ...device,
      connector_id: 'connector-terminal-down',
      hardware_id: 'e0:72:a1:d5:08:a0',
      zone_id: 'ZONE-MULTAN-01',
      zone_name: 'Multan',
      display_name: 'Multan 01',
      state: 'ONLINE',
      terminal_link: { state: 'DISCONNECTED', raw_state: 'RETRY_WAIT', since: minutesAgo(6), reason: 'RETRY_WAIT', message: 'The terminal has been unreachable for 6 minutes (RETRY_WAIT).' },
    }
    const degradedDevice: Device = {
      ...device,
      connector_id: 'connector-degraded',
      hardware_id: 'e0:72:a1:d6:f3:31',
      zone_id: 'ZONE-SWAT-01',
      zone_name: 'Swat',
      display_name: 'Swat 01',
      state: 'DEGRADED',
      terminal_link: { state: 'CONNECTED', raw_state: 'ONLINE', since: minutesAgo(90), reason: 'LINK_UP', message: 'The terminal link is up.' },
    }
    vi.stubGlobal('fetch', fetchStub([user], false, false, false, false, [device, warningDevice, terminalDownDevice, degradedDevice], {
      total: 4,
      online: 2,
      online_with_warnings: 1,
      degraded: 1,
      open_alerts: 5,
      open_unacknowledged_alerts: 2,
      terminal_attention: 1,
    }))
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })

    expect(await screen.findByText('3 of 4 online (1 with warnings)')).toBeTruthy()
    expect(screen.getByText('75%')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Operations queue: 2. 1 device degraded or offline · 1 terminal link down' })).toBeTruthy()
    expect(screen.getByLabelText('2 alerts need action')).toBeTruthy()

    const filter = screen.getByRole('combobox', { name: 'Filter by state' }) as HTMLSelectElement
    expect(Array.from(filter.options).map((option) => [option.value, option.textContent])).toEqual([
      ['ALL', 'All states'],
      ['ATTENTION', 'Needs attention'],
      ['ONLINE', 'Online'],
      ['ONLINE_WITH_WARNINGS', 'Online with warnings'],
      ['DEGRADED', 'Degraded'],
      ['OFFLINE', 'Offline'],
      ['ONBOARDING', 'Onboarding'],
      ['QUARANTINED_DUPLICATE_SERIAL', 'Quarantined'],
      ['TERMINAL_LINK_DOWN', 'Terminal link down'],
    ])

    fireEvent.click(screen.getByRole('button', { name: 'List' }))
    expect(await screen.findByRole('button', { name: 'Inspect Peshawar 02' })).toBeTruthy()
    expect(screen.getByText('Warning: add_delivery STOPPED · 37 min')).toBeTruthy()
    const terminalBadge = screen.getByLabelText('Terminal link: Disconnected')
    expect(terminalBadge.getAttribute('data-pattern')).toBe('blocked')
    expect(terminalBadge.textContent).toBe('Terminal disconnected')
    expect(terminalBadge.closest('button')?.getAttribute('aria-label')).toBe('Inspect Multan 01')
    expect(screen.queryByLabelText('Terminal link: Connected')).toBeNull()
    expect(screen.getByLabelText('Status: ONLINE WITH WARNINGS').getAttribute('data-pattern')).toBe('notice')

    fireEvent.change(filter, { target: { value: 'ATTENTION' } })
    expect(screen.getByRole('button', { name: 'Inspect Multan 01' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Inspect Swat 01' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Inspect Peshawar 02' })).toBeNull()
    expect(screen.queryByRole('button', { name: `Inspect ${device.display_name}` })).toBeNull()

    fireEvent.change(filter, { target: { value: 'TERMINAL_LINK_DOWN' } })
    expect(screen.getByRole('button', { name: 'Inspect Multan 01' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Inspect Swat 01' })).toBeNull()

    fireEvent.change(filter, { target: { value: 'ONLINE_WITH_WARNINGS' } })
    expect(screen.getByRole('button', { name: 'Inspect Peshawar 02' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Inspect Multan 01' })).toBeNull()
  })

  it('communicates every state with text, icon, and border pattern', () => {
    const { container, rerender } = render(<StatusBadge state="ONLINE" />)
    expect(container.querySelector('[data-pattern="confirmed"]')).toBeTruthy()
    expect(container.querySelector('svg')).toBeTruthy()
    expect(screen.getByText('Online')).toBeTruthy()
    rerender(<StatusBadge state="WAITING_FOR_ZKT" />)
    expect(container.querySelector('[data-pattern="waiting"]')).toBeTruthy()
    expect(screen.getByText('Waiting for ZKT')).toBeTruthy()
    const badge = screen.getByLabelText('Status: WAITING FOR ZKT')
    expect(badge.getAttribute('title')).toBe('WAITING FOR ZKT')
  })

  it('keeps the full accessible status when a long pill label must be visually truncated', () => {
    render(<StatusBadge state="SOURCE_RECOVERY_EPOCH_ACTIVATED" />)
    const badge = screen.getByLabelText('Status: SOURCE RECOVERY EPOCH ACTIVATED')
    expect(badge.getAttribute('title')).toBe('SOURCE RECOVERY EPOCH ACTIVATED')
    expect(badge.textContent).toContain('Source recovery epoch activated')
  })

  it('renders an unknown status instead of crashing on a partial API response', () => {
    const { container } = render(<StatusBadge state={undefined} />)
    expect(screen.getByText('Unknown')).toBeTruthy()
    expect(container.querySelector('[data-pattern="notice"]')).toBeTruthy()
  })

  it('maps the complete operational status taxonomy without relying on color', () => {
    const { container, rerender } = render(<StatusBadge state="COMPLETED" />)
    expect(container.querySelector('[data-pattern="confirmed"]')).toBeTruthy()
    rerender(<StatusBadge state="WARNING" />)
    expect(container.querySelector('[data-pattern="waiting"]')).toBeTruthy()
    rerender(<StatusBadge state="HIGH" />)
    expect(container.querySelector('[data-pattern="blocked"]')).toBeTruthy()
  })

  it('converts PKT attendance ranges to identical UTC bounds in every browser timezone', () => {
    expect(pktInputToUtc('2026-08-09T00:00')).toBe('2026-08-08T19:00:00.000Z')
    expect(pktInputToUtc('2026-08-09T23:59')).toBe('2026-08-09T18:59:00.000Z')
    expect(pktTodayBounds(new Date('2026-08-08T20:00:00.000Z'))).toEqual({
      from: '2026-08-09T00:00',
      to: '2026-08-09T23:59',
    })
  })


  it('returns to the login screen when any request reports an expired session', async () => {
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    vi.stubGlobal('fetch', vi.fn(async () => response({ detail: 'Session expired.' }, 401)))
    await api('/api/v1/protected-test').catch(() => undefined)
    expect(await screen.findByText(/sign in to the national device operations console/i)).toBeTruthy()
  })

  it('keeps command progress visible when a legacy command envelope has no status', () => {
    render(
      <CommandProgress
        command={{
          command_id: 'restart-command',
          type: 'command',
          status: undefined,
          created_at: '2026-07-23T12:00:00Z',
          expires_at: null,
        } as unknown as Parameters<typeof CommandProgress>[0]['command']}
        onCancel={async () => undefined}
      />,
    )
    expect(screen.getByRole('heading', { name: 'Unknown' })).toBeTruthy()
    expect(screen.getByText(/durably tracked/i)).toBeTruthy()
  })


  it('shows durable command progress without relying on hue', () => {
    const { container } = render(
      <CommandProgress
        command={{
          command_id: 'command-one',
          type: 'UPDATE_USER',
          status: 'WAITING_FOR_ZKT',
          created_at: '2026-07-13T12:00:00Z',
          expires_at: '2026-07-13T12:30:00Z',
        }}
        onCancel={async () => undefined}
      />,
    )
    expect(screen.getByRole('heading', { name: 'Waiting for ZKT' })).toBeTruthy()
    expect(screen.getByText(/durably tracked/i)).toBeTruthy()
    expect(container.querySelector('.pattern-waiting')).toBeTruthy()
    expect(screen.getByRole('button', { name: /cancel before execution/i })).toBeTruthy()
  })

  it('supports semantic keyboard tabs, Escape close, and focus restoration', async () => {
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    fireEvent.click(await screen.findByRole('button', { name: /Islamabad, 1 device, All online/i }))
    const inspect = await screen.findByRole('button', { name: `Inspect ${device.display_name}` })
    inspect.focus()
    fireEvent.click(inspect)
    expect(await screen.findByRole('dialog')).toBeTruthy()
    const overview = screen.getByRole('tab', { name: 'Overview' })
    expect(overview.getAttribute('aria-controls')).toBe('device-tabpanel')
    fireEvent.keyDown(overview, { key: 'ArrowRight' })
    const logs = screen.getByRole('tab', { name: 'Live logs' })
    expect(logs.getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('tabpanel').getAttribute('aria-labelledby')).toBe('device-tab-logs')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(inspect)
  })
})

describe('realtime refresh budget', () => {
  const calls = (fetchMock: ReturnType<typeof fetchStub>, pathname: string) =>
    fetchMock.mock.calls.filter(([input]) => new URL(String(input), 'https://add.test').pathname === pathname).length

  beforeEach(() => {
    // A request still in flight from an earlier test must not satisfy this one.
    queryClient.clear()
    vi.useFakeTimers({ shouldAdvanceTime: true })
    window.history.replaceState({}, '', '/home')
    vi.stubGlobal('EventSource', CapturingEventSource)
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    CapturingEventSource.latest = null
  })

  it('refreshes devices on device events at most every 5 s and never refetches the overview for punches', async () => {
    const fetchMock = fetchStub()
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    await waitFor(() => expect(calls(fetchMock, '/api/v1/devices')).toBe(1))
    expect(calls(fetchMock, '/api/v1/overview')).toBe(1)
    const stream = await waitFor(() => {
      expect(CapturingEventSource.latest).toBeTruthy()
      return CapturingEventSource.latest as CapturingEventSource
    })

    // A force release streams thousands of attendance batches.
    for (let index = 0; index < 40; index += 1) {
      stream.emit('attendance_batch')
      stream.emit('attendance')
      await vi.advanceTimersByTimeAsync(150)
    }
    expect(calls(fetchMock, '/api/v1/overview')).toBe(1)
    expect(calls(fetchMock, '/api/v1/devices')).toBe(1)

    // Ten heartbeats and command updates in 1.5 s: one immediate device-list
    // read and one trailing read for the rest, never one per event.
    for (let index = 0; index < 10; index += 1) {
      stream.emit(index % 2 ? 'heartbeat' : 'command_update')
      await vi.advanceTimersByTimeAsync(150)
    }
    expect(calls(fetchMock, '/api/v1/devices')).toBe(2)
    await vi.advanceTimersByTimeAsync(5000)
    await waitFor(() => expect(calls(fetchMock, '/api/v1/devices')).toBe(3))
    await vi.advanceTimersByTimeAsync(10_000)
    expect(calls(fetchMock, '/api/v1/devices')).toBe(3)
    expect(calls(fetchMock, '/api/v1/overview')).toBe(1)
    // The list never falls back to its loading state during a background refresh.
    expect(screen.queryByText(/loading live fleet/i)).toBeNull()
  })

  it('refreshes the overview for alerts at most every 15 s on the Home page', async () => {
    const fetchMock = fetchStub()
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    await waitFor(() => expect(calls(fetchMock, '/api/v1/overview')).toBe(1))
    const stream = CapturingEventSource.latest as CapturingEventSource
    const start = Date.now()

    stream.emit('alert')
    await vi.advanceTimersByTimeAsync(500)
    stream.emit('alert')
    await vi.advanceTimersByTimeAsync(Math.max(0, start + 13_000 - Date.now()))
    expect(calls(fetchMock, '/api/v1/overview')).toBe(1)
    await vi.advanceTimersByTimeAsync(Math.max(0, start + 15_500 - Date.now()))
    await waitFor(() => expect(calls(fetchMock, '/api/v1/overview')).toBe(2))
    // Alerts do not re-read the device list.
    expect(calls(fetchMock, '/api/v1/devices')).toBe(1)
  })
})

describe('overview metrics time', () => {
  it('formats the server computation time and stays silent when the backend omits it', () => {
    const base: Overview = { total: 1, open_alerts: 0, active_leases: 0 }
    expect(metricsTime(base)).toBeNull()
    expect(metricsTime({ ...base, metrics_computed_at: 'not-a-date' } as Overview)).toBeNull()
    expect(metricsTime({ ...base, metrics_computed_at: '2026-11-04T09:15:42Z' } as Overview))
      .toBe('2:15 pm') // Pakistan time without seconds, whatever the browser zone
  })
})

describe('first paint, quiet errors and live rows', () => {
  const pending = () => new Promise<Response>(() => undefined)
  const path = (input: RequestInfo | URL) => String(input)

  beforeEach(() => {
    queryClient.clear()
    window.history.replaceState({}, '', '/home')
    vi.stubGlobal('EventSource', CapturingEventSource)
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    CapturingEventSource.latest = null
    queryClient.clear()
  })

  it('shows loading tiles and no problem badge before the overview arrives, never a false 0', async () => {
    const base = fetchStub()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
      path(input).includes('/api/v1/overview') ? pending() : base(input, init)))
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    expect(screen.getByLabelText('Fleet availability: loading')).toBeTruthy()
    expect(screen.getByLabelText('ORDS delivery queue: loading')).toBeTruthy()
    expect(screen.queryByText('0%')).toBeNull()
    expect(screen.queryByText(/0 of 0 online/)).toBeNull()
    expect(document.querySelector('.nav-count')).toBeNull()
  })

  it('never pops up a message when a background refresh fails', { timeout: 15_000 }, async () => {
    const base = fetchStub()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
      path(input).startsWith('/api/v1/devices?') ? response({ detail: 'Bad gateway' }, 502) : base(input, init)))
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    // The request is retried twice before the page says so.
    expect(await screen.findByText('Could not load the devices. Trying again…', undefined, { timeout: 8000 })).toBeTruthy()
    expect(screen.queryByText(/bad gateway|request failed/i)).toBeNull()
    expect(document.querySelector('.toast')).toBeNull()
  })

  it('asks for the slim list, the quick overview off Home, and moves old addresses', async () => {
    const fetchMock = fetchStub()
    vi.stubGlobal('fetch', fetchMock)
    window.history.replaceState({}, '', '/alerts?division=life')
    render(<App />)
    await waitFor(() => expect(window.location.pathname).toBe('/problems'))
    expect(window.location.search).toBe('?division=life')
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => path(input) === '/api/v1/overview?sections=fleet')).toBe(true))
    expect(fetchMock.mock.calls.some(([input]) => path(input) === '/api/v1/devices?view=list')).toBe(true)
    expect(fetchMock.mock.calls.some(([input]) => path(input) === '/api/v1/overview')).toBe(false)
    expect(document.title).toBe('Problems · State Life Attendance')
  })

  it('opens an old device link on the Devices page', async () => {
    vi.stubGlobal('fetch', fetchStub())
    window.history.replaceState({}, '', '/fleet/connector-one')
    render(<App />)
    await waitFor(() => expect(window.location.pathname).toBe('/devices/connector-one'))
    expect(await screen.findByRole('dialog')).toBeTruthy()
  })

  it('applies a heartbeat row in place without re-reading the device list', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const fetchMock = fetchStub()
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await screen.findByRole('heading', { name: 'Fleet', level: 1 })
    fireEvent.click(screen.getByRole('button', { name: 'List' }))
    await screen.findByRole('button', { name: `Inspect ${device.display_name}` })
    const listReads = () => fetchMock.mock.calls.filter(([input]) => path(input).startsWith('/api/v1/devices?')).length
    await waitFor(() => expect(listReads()).toBe(1))
    const stream = CapturingEventSource.latest as CapturingEventSource
    const row = { ...device, display_name: 'Swat · renamed live', snapshot_at: new Date(Date.now() + 1000).toISOString(), health: { mode: 'ENFORCED', tier: 'ONLINE' } }
    stream.emit('heartbeat', JSON.stringify(row))
    await vi.advanceTimersByTimeAsync(200)
    expect(await screen.findByRole('button', { name: 'Inspect Swat · renamed live' })).toBeTruthy()
    await vi.advanceTimersByTimeAsync(6000)
    expect(listReads()).toBe(1)
    // A partial device event (no row) still re-reads the list.
    stream.emit('device', JSON.stringify({ connector_id: device.connector_id, connected: false }))
    await vi.advanceTimersByTimeAsync(200)
    await waitFor(() => expect(listReads()).toBe(2))
  })
})
