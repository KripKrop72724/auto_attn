import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AttendanceEvent, CheckinNotification, CheckinNotificationOverview, Device } from '../types'
import { AttendanceView } from './Attendance'

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

describe('Phone notification pills on the Attendance page', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/attendance')
    vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} takeRecords() { return [] } })
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('shows one pill per punch, including old punches that were never notified', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ rows, next_cursor: null, checkin_notifications: summary })))
    render(<AttendanceView {...props} />)
    const pillFor = async (name: RegExp) => within(await screen.findByRole('article', { name }, wait)).getByRole('button', { name: /Phone notification:/ })
    expect((await pillFor(/Ayesha Khan/)).textContent).toMatch(/^Notified \d{1,2}:\d{2}/)
    expect((await pillFor(/Ayesha Khan/)).getAttribute('data-notify-status')).toBe('NOTIFIED')
    expect((await pillFor(/Bilal Ahmed/)).textContent).toBe('Retrying')
    expect((await pillFor(/Chand Bibi/)).textContent).toBe('Failed')
    expect((await pillFor(/Danish Ali/)).textContent).toBe('Not sent · before feature')
    expect((await pillFor(/Erum Shah/)).textContent).toBe('Not sent · grouped')
    expect((await pillFor(/Faisal Raza/)).textContent).toBe('Pending')
    expect((await pillFor(/Chand Bibi/)).className).toContain('pattern-blocked')
    expect((await pillFor(/Ayesha Khan/)).className).toContain('pattern-confirmed')
  }, 20000)

  it('omits the pill when the API predates the feature', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ rows: [{ ...rows[0], checkin_notification: undefined }], next_cursor: null })))
    render(<AttendanceView {...props} />)
    const row = await screen.findByRole('article', { name: /Ayesha Khan/ }, wait)
    expect(within(row).queryByRole('button', { name: /Phone notification:/ })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Employee phone notifications' })).toBeNull()
  }, 20000)

  it('summarizes the last 24 hours with the current mode', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ rows, next_cursor: null, checkin_notifications: summary })))
    render(<AttendanceView {...props} />)
    const region = await screen.findByRole('region', { name: 'Employee phone notifications' }, wait)
    expect(region.textContent).toContain('Mode: All zones')
    expect(region.textContent).toContain('Working')
    expect(within(region).getByRole('list', { name: 'Last 24 hours' }).textContent).toMatch(/12 notified.*3 waiting.*1 failed.*30 not sent/)
  }, 20000)

  it('filters by phone notification status and removes the filter from its chip', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => response({ rows, next_cursor: null, checkin_notifications: summary }))
    vi.stubGlobal('fetch', fetchMock)
    render(<AttendanceView {...props} />)
    await screen.findByRole('article', { name: /Ayesha Khan/ }, wait)
    fireEvent.click(screen.getByText(/^Filters$/i))
    const fieldset = screen.getByRole('group', { name: 'Phone notification' })
    fireEvent.click(within(fieldset).getByLabelText('Failed'))
    fireEvent.click(within(fieldset).getByLabelText('Not sent'))
    await waitFor(() => {
      const sent = new URL(String(fetchMock.mock.calls.at(-1)?.[0]), 'https://add.test')
      expect(sent.searchParams.getAll('notify_status')).toEqual(['FAILED', 'NOT_SENT'])
    }, wait)
    fireEvent.click(screen.getByRole('button', { name: /Phone: Failed/ }))
    await waitFor(() => {
      const sent = new URL(String(fetchMock.mock.calls.at(-1)?.[0]), 'https://add.test')
      expect(sent.searchParams.getAll('notify_status')).toEqual(['NOT_SENT'])
    }, wait)
  }, 20000)

  it('retries a failed notification with an audited reason and password', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/checkin-notification/retry')) {
        return response({ checkin_notification: pill({ status: 'PENDING', reason: null, manual_retry: true }) })
      }
      void init
      return response({ rows, next_cursor: null, checkin_notifications: summary })
    })
    vi.stubGlobal('fetch', fetchMock)
    const notice = vi.fn()
    render(<AttendanceView {...props} toast={{ notice, error: vi.fn() }} />)
    const row = await screen.findByRole('article', { name: /Chand Bibi/ }, wait)
    fireEvent.click(within(row).getByRole('button', { name: /Phone notification: Failed/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Phone notification' }, wait)
    expect(dialog.textContent).toContain('The HRM service answered HTTP 404')
    expect(dialog.textContent).toContain('No employee #####')
    const submit = within(dialog).getByRole('button', { name: /Retry notification/ })
    expect(submit.hasAttribute('disabled')).toBe(true)
    fireEvent.change(within(dialog).getByLabelText('Audited reason'), { target: { value: 'HRM fixed the employee record' } })
    fireEvent.change(within(dialog).getByLabelText('Administrator password'), { target: { value: 'secret-password' } })
    fireEvent.click(submit)
    await waitFor(() => expect(notice).toHaveBeenCalled(), wait)
    const call = fetchMock.mock.calls.find(([input]) => String(input).includes('/checkin-notification/retry'))!
    expect(String(call[0])).toBe('/api/v1/attendance/3/checkin-notification/retry')
    expect(call[1]?.method).toBe('POST')
    expect(JSON.parse(String(call[1]?.body))).toEqual({ reason: 'HRM fixed the employee record', password: 'secret-password' })
  }, 20000)

  it('warns that a retry after an unclear answer can send the message twice', async () => {
    const uncertain = event(7, 'Gul Nawaz', pill({ status: 'FAILED', reason: 'DELIVERY_UNCERTAIN', detail: 'TRANSPORT_TIMEOUT', attempts: 1, host: 'PRIMARY', can_retry: true }))
    vi.stubGlobal('fetch', vi.fn(async () => response({ rows: [...rows, uncertain], next_cursor: null, checkin_notifications: summary })))
    render(<AttendanceView {...props} />)
    const row = await screen.findByRole('article', { name: /Gul Nawaz/ }, wait)
    fireEvent.click(within(row).getByRole('button', { name: /Phone notification: Failed/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Phone notification' }, wait)
    expect(dialog.textContent).toContain('may already have the message; it is not repeated automatically')
    expect(within(dialog).getByRole('note').textContent).toContain('Retrying can send it a second time')
    expect(within(dialog).getByRole('button', { name: /Retry notification/ })).toBeTruthy()
  }, 20000)

  it('offers no retry for a notification that was not sent', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ rows, next_cursor: null, checkin_notifications: summary })))
    render(<AttendanceView {...props} />)
    const row = await screen.findByRole('article', { name: /Danish Ali/ }, wait)
    fireEvent.click(within(row).getByRole('button', { name: /Phone notification:/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Phone notification' }, wait)
    expect(dialog.textContent).toContain('Punch is from before phone notifications started')
    expect(within(dialog).queryByRole('button', { name: /Retry notification/ })).toBeNull()
  }, 20000)

  it('switches the mode at runtime with an audited, password-confirmed change', async () => {
    const settings = {
      ...summary, mode: 'PILOT', revision: 4, pilot_zone_ids: ['ZONE-ONE'],
      pilot_cnics: [{ id: 'abcd1234abcd1234', masked: '*****-****567-1' }], updated_by: 'StateHealthAdmin',
      updated_at: null, endpoint_host: 'local.slichealth.com', fallback_host: 'eclaim2.slichealth.com',
      cnic_json_type: 'number', stale_window_hours: 12, coalesce_seconds: 120, retry_window_hours: 6,
      modes: ['OFF', 'PILOT', 'ALL'], punch_policies: ['ALL_PUNCHES', 'FIRST_PER_DAY'],
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/v1/checkin-notifications') return response(settings)
      if (String(input) === '/api/v1/checkin-notifications/settings') return response({ ...settings, mode: 'ALL', revision: 5 })
      void init
      return response({ rows, next_cursor: null, checkin_notifications: summary })
    })
    vi.stubGlobal('fetch', fetchMock)
    const notice = vi.fn()
    render(<AttendanceView {...props} toast={{ notice, error: vi.fn() }} />)
    const region = await screen.findByRole('region', { name: 'Employee phone notifications' }, wait)
    fireEvent.click(within(region).getByRole('button', { name: /Settings/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Phone notification settings' }, wait)
    await within(dialog).findByRole('radio', { name: /^All zones/ }, wait)
    expect((within(dialog).getByRole('radio', { name: /^Pilot/ }) as HTMLInputElement).checked).toBe(true)
    expect((within(dialog).getByLabelText('Pilot zone IDs') as HTMLInputElement).value).toBe('ZONE-ONE')
    fireEvent.click(within(dialog).getByRole('radio', { name: /^All zones/ }))
    fireEvent.click(within(dialog).getByLabelText('Remove *****-****567-1'))
    fireEvent.change(within(dialog).getByLabelText('Add employee CNIC'), { target: { value: '35202-1234567-1' } })
    fireEvent.change(within(dialog).getByLabelText('Audited reason'), { target: { value: 'Owner approved rollout to every zone' } })
    fireEvent.change(within(dialog).getByLabelText('Administrator password'), { target: { value: 'secret-password' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /Save settings/ }))
    await waitFor(() => expect(notice).toHaveBeenCalledWith(expect.stringContaining('all zones')), wait)
    const call = fetchMock.mock.calls.find(([input]) => String(input) === '/api/v1/checkin-notifications/settings')!
    expect(call[1]?.method).toBe('PUT')
    expect(JSON.parse(String(call[1]?.body))).toEqual({
      mode: 'ALL', punch_policy: 'ALL_PUNCHES', pilot_zone_ids: ['ZONE-ONE'],
      add_pilot_cnics: ['3520212345671'], remove_pilot_cnic_ids: ['abcd1234abcd1234'],
      expected_revision: 4, reason: 'Owner approved rollout to every zone', password: 'secret-password',
    })
  }, 20000)
})
