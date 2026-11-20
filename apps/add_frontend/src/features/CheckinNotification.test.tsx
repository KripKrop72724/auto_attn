import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AttendanceEvent, CheckinNotification, CheckinNotificationOverview, Device } from '../types'
import { BrowserRouter } from 'react-router-dom'
import { AttendanceView } from './Attendance'
import { CheckinNotifySettingsCard } from './CheckinNotification'
import { queryClient } from '../data'
import { autoReleaseQueryKey } from './unknownAutoRelease'
import { expectPlainText } from '../words/testing'

const device: Device = {
  connector_id: 'connector-one', hardware_id: 'hw-one', zone_id: 'ZONE-ONE', zone_name: 'Islamabad',
  device_id: '1', display_name: 'SLICTOWER · 3rd Floor', state: 'ONLINE', connected: true,
  firmware_version: '2.4.5', onboarding_generation: 2, last_onboarded_at: null,
  last_seen_at: '2026-08-12T10:00:00Z', current_activity: 'Live capture', last_error_code: null,
  zkt: null,
} as unknown as Device

const pill = (overrides: Partial<CheckinNotification> = {}): CheckinNotification => ({
  status: 'NOT_SENT', reason: 'BEFORE_FEATURE', detail: null, sent_at: null, attempts: 0,
  next_attempt_at: null, last_http_status: null, host: null, updated_at: null,
  can_retry: false, manual_retry: false, ...overrides,
})

const event = (id: number, name: string, notification: CheckinNotification | null): AttendanceEvent => ({
  id, event_uid: `event-${id}`, device_serial: 'ADZV211860253', uid: String(id), user_id: String(1000 + id),
  display_name: name, cnic_masked: '*****-****567-1', device_event_time: '2026-10-22T03:00:00Z',
  captured_at: '2026-10-22T03:00:01Z', received_at: '2026-10-22T03:00:02Z', source: 'LIVE',
  status: '0', punch: '0', clock_quality: 'OK', clock_drift_seconds: 0, ords_status: 'ACKED',
  oracle_confirmed_at: '2026-10-22T03:00:05Z', oracle_confirmation_path: 'ADD_DELIVERY',
  identity_resolution_id: null, checkin_notification: notification,
})

const summary: CheckinNotificationOverview = {
  state: 'OK', configured: true, mode: 'ALL', punch_policy: 'ALL_PUNCHES', pilot_zone_count: 0,
  pilot_cnic_count: 0, last_success_at: '2026-10-22T03:00:10Z', last_failure_at: null,
  last_failure_category: null, consecutive_failures: 0, preferred_host: 'PRIMARY',
  primary_reprobe_at: null, paused_until: null, oldest_pending_at: null, oldest_pending_age_seconds: 0,
  started_at: '2026-10-22T00:00:00Z',
  last_24h: { notified: 12, pending: 1, retrying: 2, failed: 1, not_sent: 30, not_sent_reasons: { OLD_PUNCH: 30 }, window_hours: 24 },
}

const rows = [
  event(1, 'Ayesha Khan', pill({ status: 'NOTIFIED', reason: null, sent_at: '2026-10-22T03:00:09Z', attempts: 1, host: 'PRIMARY' })),
  event(2, 'Bilal Ahmed', pill({ status: 'RETRYING', reason: 'HTTP_503', attempts: 2, next_attempt_at: '2026-10-22T03:05:00Z' })),
  event(3, 'Chand Bibi', pill({ status: 'FAILED', reason: 'HTTP_404', detail: 'No employee #####', attempts: 1, last_http_status: 404, can_retry: true })),
  event(4, 'Danish Ali', pill()),
  event(5, 'Erum Shah', pill({ reason: 'GROUPED' })),
  event(6, 'Faisal Raza', pill({ status: 'PENDING', reason: null })),
]

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

const props = { devices: [device], revision: 0, realtimeState: 'live' as const, realtimeLastSyncAt: null }
const wait = { timeout: 5000 }
const renderView = (extra: Partial<Parameters<typeof AttendanceView>[0]> = {}) =>
  render(<AttendanceView {...props} {...extra} />, { wrapper: BrowserRouter })
/** The list answers with `rows`; the totals and journey calls get shapes of their own. */
const stub = (handler?: (input: RequestInfo | URL, init?: RequestInit) => Response | undefined) => vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const handled = handler?.(input, init)
  if (handled) return handled
  const url = new URL(String(input), 'https://add.test')
  if (url.pathname === '/api/v1/attendance/summary') return response({ totals: { received: 6 }, by_division: {}, computed_at: new Date().toISOString() })
  if (url.pathname.endsWith('/journey')) return response({ detail: 'Not found' }, 404)
  return response({ rows, next_cursor: null })
})
const openSheet = async (name: RegExp) => {
  const row = await screen.findByRole('article', { name }, wait)
  fireEvent.click(within(row).getByRole('button', { name: /^Open check-in/ }))
  return screen.findByRole('dialog', {}, wait)
}

describe('Phone messages on the Attendance page', () => {
  beforeEach(() => {
    queryClient.setQueryData(autoReleaseQueryKey, null)
    window.history.replaceState(null, '', '/attendance')
    vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} takeRecords() { return [] } })
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    queryClient.clear()
  })

  it('marks only a failed message on the list and shows each state in the sheet', async () => {
    vi.stubGlobal('fetch', stub())
    renderView()
    const chand = await screen.findByRole('article', { name: /Chand Bibi/ }, wait)
    expect(within(chand).getByText('Phone message failed')).toBeTruthy()
    expect(within(await screen.findByRole('article', { name: /Ayesha Khan/ })).queryByText(/Phone message/)).toBeNull()
    expect(screen.queryByRole('region', { name: /phone messages/i })).toBeNull()
    const ayesha = await openSheet(/Ayesha Khan/)
    expect(within(ayesha).getByLabelText(/^Phone message: Delivered \d{1,2}:\d{2} (am|pm)$/)).toBeTruthy()
    fireEvent.click(within(ayesha).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const danish = await openSheet(/Danish Ali/)
    expect(within(danish).getByText('Not sent: older check-in')).toBeTruthy()
    expect(within(danish).queryByRole('button', { name: /Send the message again/ })).toBeNull()
    fireEvent.click(within(danish).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const erum = await openSheet(/Erum Shah/)
    expect(within(erum).getByText('Not sent: combined with an earlier one')).toBeTruthy()
  }, 20000)

  it('shows nothing about phone messages when the server predates them', async () => {
    vi.stubGlobal('fetch', stub(() => response({ rows: [{ ...rows[0], checkin_notification: undefined }], next_cursor: null })))
    renderView()
    const sheet = await openSheet(/Ayesha Khan/)
    expect(within(sheet).queryByText('Phone message')).toBeNull()
  }, 20000)

  it('filters by phone message state and removes the filter from its chip', async () => {
    const fetchMock = stub()
    vi.stubGlobal('fetch', fetchMock)
    renderView()
    await screen.findByRole('article', { name: /Ayesha Khan/ }, wait)
    fireEvent.click(screen.getByRole('button', { name: /More filters/ }))
    const fieldset = await screen.findByRole('group', { name: 'Phone message' })
    fireEvent.click(within(fieldset).getByLabelText('Failed'))
    fireEvent.click(within(fieldset).getByLabelText('Not sent'))
    const lastList = () => fetchMock.mock.calls.map(([input]) => new URL(String(input), 'https://add.test')).filter((url) => url.pathname === '/api/v1/attendance').at(-1)!
    await waitFor(() => expect(lastList().searchParams.getAll('notify_status')).toEqual(['FAILED', 'NOT_SENT']), wait)
    expect(window.location.search).toContain('phone=failed%2Cnot_sent')
    fireEvent.click(screen.getByRole('button', { name: 'Remove filter: Phone message: failed' }))
    await waitFor(() => expect(lastList().searchParams.getAll('notify_status')).toEqual(['NOT_SENT']), wait)
  }, 20000)

  it('sends a failed message again with a reason and password (unchanged)', async () => {
    const fetchMock = stub((input) => (String(input).includes('/checkin-notification/retry')
      ? response({ checkin_notification: pill({ status: 'PENDING', reason: null, manual_retry: true }) })
      : undefined))
    vi.stubGlobal('fetch', fetchMock)
    const notice = vi.fn()
    renderView({ toast: { notice, error: vi.fn() } })
    const sheet = await openSheet(/Chand Bibi/)
    expect(within(sheet).getByText(/^Failed: the phone message service refused it/)).toBeTruthy()
    expect(within(sheet).getByText('No employee #####').closest('[data-technical]')).toBeTruthy()
    fireEvent.click(within(sheet).getByRole('button', { name: /Send the message again/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Send the message again' }, wait)
    const submit = within(dialog).getByRole('button', { name: /Send the message again/ })
    expect(submit.hasAttribute('disabled')).toBe(true)
    fireEvent.change(within(dialog).getByLabelText('Why?'), { target: { value: 'HRM fixed the employee record' } })
    fireEvent.change(within(dialog).getByLabelText('Your password'), { target: { value: 'secret-password' } })
    fireEvent.click(submit)
    await waitFor(() => expect(notice).toHaveBeenCalled(), wait)
    const call = fetchMock.mock.calls.find(([input]) => String(input).includes('/checkin-notification/retry'))!
    expect(String(call[0])).toBe('/api/v1/attendance/3/checkin-notification/retry')
    expect(call[1]?.method).toBe('POST')
    expect(JSON.parse(String(call[1]?.body))).toEqual({ reason: 'HRM fixed the employee record', password: 'secret-password' })
  }, 20000)

  it('warns that sending again after an unclear answer can send the message twice', async () => {
    const uncertain = event(7, 'Gul Nawaz', pill({ status: 'FAILED', reason: 'DELIVERY_UNCERTAIN', detail: 'TRANSPORT_TIMEOUT', attempts: 1, host: 'PRIMARY', can_retry: true }))
    vi.stubGlobal('fetch', stub(() => response({ rows: [...rows, uncertain], next_cursor: null })))
    renderView()
    const sheet = await openSheet(/Gul Nawaz/)
    fireEvent.click(within(sheet).getByRole('button', { name: /Send the message again/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Send the message again' }, wait)
    expect(dialog.textContent).toContain('may already have the message; it is not repeated automatically')
    expect(within(dialog).getByRole('note').textContent).toContain('Sending again can send it a second time')
  }, 20000)
})

describe('Settings › Check-in phone messages', () => {
  const settings = {
    ...summary, mode: 'PILOT', revision: 4, pilot_zone_ids: ['ZONE-ONE'],
    pilot_cnics: [{ id: 'abcd1234abcd1234', masked: '*****-****567-1' }], updated_by: 'StateHealthAdmin',
    updated_at: null, endpoint_host: 'local.slichealth.com', fallback_host: 'eclaim2.slichealth.com',
    cnic_json_type: 'number', stale_window_hours: 12, coalesce_seconds: 120, retry_window_hours: 6,
    modes: ['OFF', 'PILOT', 'ALL'], punch_policies: ['ALL_PUNCHES', 'FIRST_PER_DAY'],
  }
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  it('reads its own figures and shows the last 24 hours as a 2×2 grid', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ ...settings, mode: 'ALL' })))
    const { container } = render(<CheckinNotifySettingsCard toast={{ notice: vi.fn(), error: vi.fn() }} />)
    const region = await screen.findByRole('region', { name: 'Check-in phone messages' })
    expect(await within(region).findByText('Sending to: everyone · every check-in')).toBeTruthy()
    expect(within(region).getByLabelText('Status: Working')).toBeTruthy()
    const counts = within(region).getByLabelText('Last 24 hours')
    // A definition grid, not a wrapping list whose flex basis became a tall empty block on phones.
    expect(counts.tagName).toBe('DL')
    expect(counts.className).toBe('notify-card-counts')
    expect(counts.textContent).toMatch(/Delivered12.*Waiting3.*Failed1.*Not needed30/)
    expect(within(region).getByText(/local\.slichealth\.com/).closest('[data-technical]')).toBeTruthy()
    expectPlainText(container)
  })

  it('says when phone messages are not set up and when it could not load', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ detail: 'no' }, 503)))
    render(<CheckinNotifySettingsCard toast={{ notice: vi.fn(), error: vi.fn() }} />)
    expect(await screen.findByText(/Couldn't load phone message settings/)).toBeTruthy()
    cleanup()
    vi.stubGlobal('fetch', vi.fn(async () => response({ ...settings, configured: false, state: 'NOT_CONFIGURED' })))
    render(<CheckinNotifySettingsCard toast={{ notice: vi.fn(), error: vi.fn() }} />)
    expect(await screen.findByText("Phone messages aren't set up yet, so nothing is sent.")).toBeTruthy()
  })

  it('changes who gets messages with a reason and password (unchanged request)', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      if (String(input) === '/api/v1/checkin-notifications') return response(settings)
      if (String(input) === '/api/v1/checkin-notifications/settings') return response({ ...settings, mode: 'ALL', revision: 5 })
      return response({})
    })
    vi.stubGlobal('fetch', fetchMock)
    const notice = vi.fn()
    render(<CheckinNotifySettingsCard toast={{ notice, error: vi.fn() }} />)
    const region = await screen.findByRole('region', { name: 'Check-in phone messages' })
    expect(await within(region).findByText(/Sending to: trial \(0 areas, 0 people\)/)).toBeTruthy()
    fireEvent.click(within(region).getByRole('button', { name: /Change settings/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Phone message settings' }, wait)
    await within(dialog).findByRole('radio', { name: /^Everyone/ }, wait)
    expect((within(dialog).getByRole('radio', { name: /^Trial/ }) as HTMLInputElement).checked).toBe(true)
    expect((within(dialog).getByLabelText('Trial areas') as HTMLInputElement).value).toBe('ZONE-ONE')
    fireEvent.click(within(dialog).getByRole('radio', { name: /^Everyone/ }))
    fireEvent.click(within(dialog).getByLabelText('Remove *****-****567-1'))
    fireEvent.change(within(dialog).getByLabelText("Add a person's CNIC"), { target: { value: '35202-1234567-1' } })
    fireEvent.change(within(dialog).getByLabelText('Why?'), { target: { value: 'Owner approved rollout to every zone' } })
    fireEvent.change(within(dialog).getByLabelText('Your password'), { target: { value: 'secret-password' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /Save settings/ }))
    await waitFor(() => expect(notice).toHaveBeenCalledWith('Phone messages: now Everyone.'), wait)
    const call = fetchMock.mock.calls.find(([input]) => String(input) === '/api/v1/checkin-notifications/settings')!
    expect(call[1]?.method).toBe('PUT')
    expect(JSON.parse(String(call[1]?.body))).toEqual({
      mode: 'ALL', punch_policy: 'ALL_PUNCHES', pilot_zone_ids: ['ZONE-ONE'],
      add_pilot_cnics: ['3520212345671'], remove_pilot_cnic_ids: ['abcd1234abcd1234'],
      expected_revision: 4, reason: 'Owner approved rollout to every zone', password: 'secret-password',
    })
  })

  it('offers the trial areas grouped Life and Health and keeps the same area codes', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(settings)))
    const devices = [
      { ...device, connector_id: 'life', zone_id: 'LF-ZONE-BLD9', zone_name: 'LF-ZONE-BLD9', division: 'life' },
      { ...device, connector_id: 'health', zone_id: 'ZONE-ONE', zone_name: 'Islamabad', division: 'health' },
    ] as Device[]
    render(<CheckinNotifySettingsCard toast={{ notice: vi.fn(), error: vi.fn() }} devices={devices} />)
    fireEvent.click(await screen.findByRole('button', { name: /Change settings/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Phone message settings' }, wait)
    const areas = await within(dialog).findByRole('group', { name: 'Trial areas' })
    expect((within(areas).getByLabelText('Islamabad') as HTMLInputElement).checked).toBe(true)
    expect((within(areas).getByLabelText('Building 9') as HTMLInputElement).checked).toBe(false)
    expect(areas.textContent).toMatch(/Life.*Building 9.*Health.*Islamabad/)
  })
})
