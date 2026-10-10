import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { useToast } from '../App'
import type { Alert, AlertQueueResponse, Device } from '../types'
import { AlertsView } from './Monitoring'

const device: Device = {
  connector_id: 'connector-peshawar-02',
  hardware_id: 'e0:72:a1:d7:05:c4',
  zone_id: 'ZONE-PESHAWAR-02',
  zone_name: 'Peshawar',
  device_id: '2',
  display_name: 'Peshawar 02',
  state: 'ONLINE_WITH_WARNINGS',
  connected: true,
  firmware_version: '2.5.2',
  onboarding_generation: 3,
  last_onboarded_at: null,
  last_seen_at: '2026-10-10T08:00:00Z',
  current_activity: 'LIVE_CAPTURE',
  last_error_code: null,
  zkt: null,
}

const deviceRef = { connector_id: device.connector_id, display_name: device.display_name, zone_id: device.zone_id, hardware_id: device.hardware_id }

const alert = (overrides: Partial<Alert>): AlertQueueResponse['rows'][number] => ({
  id: 41,
  code: 'ESP_DELIVERY_WORKER_FAULT',
  severity: 'HIGH',
  state: 'OPEN',
  message: 'Delivery worker add_delivery reported STOPPED.',
  details: { binding: 'INFERRED_PREVIOUS', firmware_version: '2.6.27', evidence: { summary: 'add_delivery STOPPED' } },
  first_seen_at: '2026-10-09T08:00:00Z',
  last_seen_at: '2026-10-09T09:00:00Z',
  acknowledged_at: null,
  acknowledged_by: null,
  resolved_at: null,
  resolution: null,
  device: deviceRef,
  ...overrides,
})

const needsAction = alert({})
const acknowledged = alert({
  id: 43,
  code: 'ZKT_CLOCK_DRIFT',
  severity: 'WARNING',
  message: 'Terminal clock drift exceeds two minutes.',
  details: { acknowledged_by: 'StateHealthAdmin', acknowledgement_note: 'Clock reset scheduled' },
  acknowledged_at: '2026-10-10T07:30:00Z',
  acknowledged_by: 'StateHealthAdmin',
})
const resolved = alert({
  id: 40,
  state: 'RESOLVED',
  message: 'Delivery worker ords_delivery reported STOPPED.',
  resolved_at: '2026-10-10T06:00:00Z',
  details: { resolution: { kind: 'CLEANUP', actor: 'StateHealthAdmin', reason: 'Stranded by the 2.6.27 recovery image.', at: '2026-10-10T06:00:00Z' } },
  resolution: { kind: 'CLEANUP', actor: 'StateHealthAdmin', reason: 'Stranded by the 2.6.27 recovery image.', at: '2026-10-10T06:00:00Z' },
})

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const queues: Record<string, AlertQueueResponse['rows']> = {
  NEEDS_ACTION: [needsAction],
  ACKNOWLEDGED: [acknowledged],
  RESOLVED: [resolved],
  ALL: [needsAction, acknowledged, resolved],
}
let resolveResponses: Response[] = []

const toast = () => ({ notice: vi.fn(), error: vi.fn() }) as unknown as ReturnType<typeof useToast>

describe('Alerts queues', () => {
  beforeEach(() => {
    resolveResponses = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost')
      if (url.pathname === '/api/v1/alerts') {
        return json({
          rows: queues[url.searchParams.get('queue') || 'ALL'],
          next_cursor: null,
          totals: { all: 6, open: 2, acknowledged: 0, resolved: 4 },
          queue_totals: { needs_action: 1, acknowledged: 1, resolved: 4, all: 6 },
        })
      }
      if (url.pathname === '/api/v1/attendance-quarantine') return json({ totals: { all: 0, open: 0 }, filtered_total: 0, rows: [], next_cursor: null })
      if (/^\/api\/v1\/alerts\/\d+\/resolve$/.test(url.pathname) && init?.method === 'POST') {
        return resolveResponses.shift() || json({ alert: { ...needsAction, state: 'RESOLVED' }, device_state: 'ONLINE', device_error: null, residual_alert_id: null })
      }
      if (/^\/api\/v1\/alerts\/\d+\/acknowledge$/.test(url.pathname) && init?.method === 'POST') return json(acknowledged)
      throw new Error(`Unexpected request: ${url.pathname}${url.search}`)
    }))
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  const alertRequests = () => vi.mocked(fetch).mock.calls
    .map(([input]) => new URL(String(input), 'http://localhost'))
    .filter((url) => url.pathname === '/api/v1/alerts')

  it('loads queues with the queue parameter and keeps acknowledged alerts visible', async () => {
    render(<AlertsView devices={[device]} toast={toast()} revision={0} />)
    expect(await screen.findByRole('heading', { name: needsAction.message })).toBeTruthy()
    expect(alertRequests()[0].searchParams.get('queue')).toBe('NEEDS_ACTION')
    expect(alertRequests()[0].searchParams.has('state')).toBe(false)
    const queueGroup = screen.getByRole('group', { name: 'Alert queue' })
    expect(within(queueGroup).getByRole('button', { name: 'Needs action 1' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByText(/Evidence: add_delivery STOPPED · Firmware 2\.6\.27 · Inferred on an earlier boot/)).toBeTruthy()

    fireEvent.click(within(queueGroup).getByRole('button', { name: 'Acknowledged 1' }))
    const card = await screen.findByRole('article', { name: acknowledged.message })
    expect(alertRequests().at(-1)?.searchParams.get('queue')).toBe('ACKNOWLEDGED')
    expect(card.className).toContain('pattern-notice')
    expect(within(card).getByText('Acknowledged')).toBeTruthy()
    expect(within(card).getByText(/^Acknowledged by StateHealthAdmin · .+ · Clock reset scheduled$/)).toBeTruthy()
    expect(within(card).queryByRole('button', { name: /Acknowledge/ })).toBeNull()
    expect(within(card).getByRole('button', { name: /Resolve/ })).toBeTruthy()

    fireEvent.click(within(queueGroup).getByRole('button', { name: 'Resolved 4' }))
    const resolvedCard = await screen.findByRole('article', { name: resolved.message })
    expect(alertRequests().at(-1)?.searchParams.get('queue')).toBe('RESOLVED')
    expect(resolvedCard.className).toContain('pattern-confirmed')
    expect(within(resolvedCard).getByText(/Resolved by stranded-alert cleanup \(StateHealthAdmin\)/)).toBeTruthy()
    expect(within(resolvedCard).getByText(/Reason: Stranded by the 2\.6\.27 recovery image\./)).toBeTruthy()
    expect(within(resolvedCard).queryByRole('button', { name: /Resolve/ })).toBeNull()

    fireEvent.click(within(queueGroup).getByRole('button', { name: 'All 6' }))
    await waitFor(() => expect(alertRequests().at(-1)?.searchParams.get('queue')).toBe('ALL'))
  })

  it('requires a reason and password to resolve, explains a 409 and keeps one idempotency key', async () => {
    resolveResponses = [json({
      detail: {
        code: 'ALERT_CONDITION_CURRENT',
        message: 'The latest evidence still asserts this condition.',
        clear_condition: 'Clears when every required delivery worker reports running with a fresh tick.',
      },
    }, 409)]
    const notices = toast()
    render(<AlertsView devices={[device]} toast={notices} revision={0} />)
    const card = await screen.findByRole('article', { name: needsAction.message })
    fireEvent.click(within(card).getByRole('button', { name: /Resolve/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Resolve alert with reason' })
    const submit = within(dialog).getByRole('button', { name: 'Resolve alert' }) as HTMLButtonElement
    const reason = within(dialog).getByLabelText('Audited reason')
    const password = within(dialog).getByLabelText('Administrator password') as HTMLInputElement
    expect(submit.disabled).toBe(true)
    fireEvent.change(reason, { target: { value: 'Too short' } })
    fireEvent.change(password, { target: { value: 'local-step-up' } })
    expect(submit.disabled).toBe(true)
    expect(within(dialog).getByText('9 / 500 characters · 1 more needed')).toBeTruthy()
    fireEvent.change(reason, { target: { value: 'Raised under the 2.6.27 recovery image; 2.5.2 cannot report workers.' } })
    expect(submit.disabled).toBe(false)
    fireEvent.click(submit)

    expect(await within(dialog).findByText(/The device still reports this condition/)).toBeTruthy()
    expect(within(dialog).getByText('The latest evidence still asserts this condition.')).toBeTruthy()
    expect(within(dialog).getByText('Clears when every required delivery worker reports running with a fresh tick.')).toBeTruthy()
    expect(password.value).toBe('')
    expect(submit.disabled).toBe(true)

    fireEvent.change(password, { target: { value: 'local-step-up' } })
    fireEvent.click(submit)
    await waitFor(() => expect(notices.notice).toHaveBeenCalledWith(expect.stringMatching(/Alert resolved with an audit entry/)))
    expect(screen.queryByRole('dialog', { name: 'Resolve alert with reason' })).toBeNull()
    const posts = vi.mocked(fetch).mock.calls.filter(([input, init]) => String(input).endsWith('/api/v1/alerts/41/resolve') && init?.method === 'POST')
    expect(posts).toHaveLength(2)
    const bodies = posts.map(([, init]) => JSON.parse(String(init?.body)))
    expect(bodies[1]).toEqual({
      reason: 'Raised under the 2.6.27 recovery image; 2.5.2 cannot report workers.',
      password: 'local-step-up',
      idempotency_key: expect.stringMatching(/^alert-resolve:/),
    })
    expect(bodies[0].idempotency_key).toBe(bodies[1].idempotency_key)
  })

  it('acknowledges without closing the alert and opens the stranded alert review', async () => {
    const notices = toast()
    render(<AlertsView devices={[device]} toast={notices} revision={0} />)
    const card = await screen.findByRole('article', { name: needsAction.message })
    fireEvent.click(within(card).getByRole('button', { name: /Acknowledge/ }))
    await waitFor(() => expect(notices.notice).toHaveBeenCalledWith(expect.stringMatching(/stays active until its condition clears/)))
    const acknowledgement = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith('/api/v1/alerts/41/acknowledge'))
    expect(acknowledgement?.[1]?.method).toBe('POST')

    fireEvent.click(screen.getByRole('button', { name: /Stranded alert review/ }))
    expect(await screen.findByRole('dialog', { name: 'Stranded alert review' })).toBeTruthy()
    expect(screen.getByRole('radio', { name: /Peshawar 02 \+ 06/ })).toHaveProperty('checked', true)
  })
})
