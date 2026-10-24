import { act, cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { formatDuration, formatSince, isPublicStatusPath, type PublicStatusSnapshot } from '../publicStatus'
import PublicStatus from './PublicStatus'

class Stream extends EventTarget {
  static latest: Stream
  onerror: (() => void) | null = null
  close = vi.fn()
  constructor(readonly url: string) {
    super()
    Stream.latest = this
  }
}

const snapshot = (overrides: Partial<PublicStatusSnapshot> = {}): PublicStatusSnapshot => ({
  updated_at: new Date().toISOString(),
  total: 3,
  online: 2,
  offline: 1,
  zones: [
    { zone: 'life', name: 'Life', total: 2, online: 1, offline: 1 },
    { zone: 'health', name: 'Health', total: 1, online: 1, offline: 0 },
  ],
  devices: [
    { name: 'Building 9 · Device 1', location: 'Building 9', unit: 'Device 1', zone: 'life', code: 'LF-ZONE-BLD9-01', status: 'online', since: '2026-10-24T07:03:00Z' },
    { name: 'Building 9 · Device 3', location: 'Building 9', unit: 'Device 3', zone: 'life', code: 'LF-ZONE-BLD9-03', status: 'offline', since: '2026-10-21T10:17:00Z' },
    { name: 'Swat', location: 'Swat', unit: null, zone: 'health', code: 'ZONE-SWAT-01', status: 'online', since: '2026-10-24T07:03:00Z' },
  ],
  ...overrides,
})

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('public status page', () => {
  it('shows Life and Health with plain Online and Offline, without signing in', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(snapshot())))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('EventSource', Stream)
    render(<PublicStatus />)

    const life = await screen.findByRole('region', { name: 'Life' })
    expect(fetchMock).toHaveBeenCalledWith('/api/public/v1/status', { cache: 'no-store' })
    expect(Stream.latest.url).toBe('/events/public/v1/status')
    within(life).getByText('1 of 2 online')
    within(life).getByText('1 offline')
    const offline = within(life).getByText('Building 9 · Device 3').closest('li')!
    within(offline).getByText('Offline')
    expect(offline.getAttribute('title')).toBe('LF-ZONE-BLD9-03')
    const health = screen.getByRole('region', { name: 'Health' })
    within(health).getByText('All online')
    within(health).getByText('Online')
    expect(screen.queryByText(/degraded|warning/i)).toBeNull()
    expect(document.title).toBe('Attendance Device Status · State Life')
  })

  it('applies each live update from the stream', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(snapshot()))))
    vi.stubGlobal('EventSource', Stream)
    render(<PublicStatus />)
    await screen.findByRole('region', { name: 'Life' })
    expect(screen.getByRole('status').textContent).toContain('Connecting')

    const recovered = snapshot({
      online: 3, offline: 0,
      zones: [
        { zone: 'life', name: 'Life', total: 2, online: 2, offline: 0 },
        { zone: 'health', name: 'Health', total: 1, online: 1, offline: 0 },
      ],
    })
    recovered.devices[1] = { ...recovered.devices[1], status: 'online', since: new Date().toISOString() }
    act(() => { Stream.latest.dispatchEvent(new MessageEvent('status', { data: JSON.stringify(recovered) })) })
    expect(screen.getByRole('status').textContent).toContain('Live')
    within(screen.getByRole('region', { name: 'Life' })).getByText('All online')

    act(() => { Stream.latest.onerror?.() })
    expect(screen.getByRole('status').textContent).toContain('Reconnecting')
  })

  it('says so when status cannot be loaded', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 503 })))
    vi.stubGlobal('EventSource', Stream)
    render(<PublicStatus />)
    expect((await screen.findByRole('alert')).textContent).toContain('Device status is unavailable right now')
  })
})

describe('public status formatting', () => {
  const now = new Date('2026-10-24T11:30:00Z') // 4:30 PM in Pakistan

  it('dates "since" in Pakistan time', () => {
    expect(formatSince('2026-10-24T07:03:00Z', now)).toBe('12:03 PM')
    expect(formatSince('2026-10-23T12:00:00Z', now)).toBe('yesterday, 5:00 PM')
    expect(formatSince('2026-10-21T10:17:00Z', now)).toBe('21 Oct, 3:17 PM')
    expect(formatSince('2025-12-30T10:17:00Z', now)).toBe('30 Dec 2025, 3:17 PM')
  })

  it('states how long in plain words', () => {
    expect(formatDuration('2026-10-24T11:29:40Z', now)).toBe('less than a minute')
    expect(formatDuration('2026-10-24T11:18:00Z', now)).toBe('12 min')
    expect(formatDuration('2026-10-24T08:10:00Z', now)).toBe('3 h 20 min')
    expect(formatDuration('2026-10-23T07:30:00Z', now)).toBe('1 day 4 h')
    expect(formatDuration('2026-10-12T11:30:00Z', now)).toBe('12 days')
  })

  it('serves the page at /status only', () => {
    expect(isPublicStatusPath('/status')).toBe(true)
    expect(isPublicStatusPath('/status/')).toBe(true)
    expect(isPublicStatusPath('/fleet')).toBe(false)
    expect(isPublicStatusPath('/status/extra')).toBe(false)
  })
})
