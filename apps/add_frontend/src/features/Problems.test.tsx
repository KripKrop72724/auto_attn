import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../ui'
import { reloadPrefsForTests, resetPrefs } from '../lib/prefs'
import { expectPlainText } from '../words/testing'
import type { Device } from '../types'
import { ProblemsView, groupByDevice } from './Problems'
import type { ProblemCounts, ProblemRow, ProblemsResponse } from './problems/types'

const device: Device = {
  connector_id: 'connector-peshawar-02',
  hardware_id: 'e0:72:a1:d7:05:c4',
  zone_id: 'ZONE-PESHAWAR-02',
  zone_name: 'Peshawar',
  device_id: '2',
  display_name: 'ZONE-PESHAWAR-02',
  division: 'health',
  friendly_name: 'Peshawar · Device 2',
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
const lifeDevice: Device = { ...device, connector_id: 'connector-bld9-03', zone_id: 'LF-ZONE-BLD9-03', display_name: 'LF-ZONE-BLD9-03', division: 'life', friendly_name: 'Building 9 · Device 3', hardware_id: 'e0:72:a1:d7:05:c9' }

const ref = (from: Device) => ({ connector_id: from.connector_id, display_name: from.display_name, zone_id: from.zone_id, hardware_id: from.hardware_id, division: from.division, friendly_name: from.friendly_name })

const alert = (overrides: Partial<ProblemRow>): ProblemRow => ({
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
  plain_title: 'Link box is stuck sending attendance',
  plain_detail: 'The part of the link box that sends check-ins has stopped.',
  next_step: 'Wait 10 minutes. If it stays, call IT.',
  type: 'link_box',
  affects_attendance: true,
  operator: { resolvable: true, refusal_code: null, refusal: null },
  device: ref(device),
  ...overrides,
})

const needsAction = alert({})
const workflow = alert({
  id: 44,
  code: 'ORDS_DELIVERY_FAILED',
  severity: 'WARNING',
  message: 'Oracle attendance delivery is retrying.',
  plain_title: 'Check-ins not reaching the HR system (trying again)',
  plain_detail: 'Check-ins from this place are saved and are being sent again automatically.',
  next_step: 'Nothing at the place. If it lasts more than an hour, tell IT.',
  operator: { resolvable: false, refusal_code: 'ALERT_WORKFLOW_OWNED', refusal: 'Its own review workflow resolves this alert.' },
  device: ref(lifeDevice),
})
const acknowledged = alert({
  id: 43,
  code: 'ZKT_CLOCK_DRIFT',
  severity: 'WARNING',
  message: 'Terminal clock drift exceeds two minutes.',
  plain_title: 'Machine clock is 4 min fast',
  details: { acknowledged_by: 'StateHealthAdmin', acknowledgement_note: 'Clock reset scheduled' },
  acknowledged_at: '2026-10-10T07:30:00Z',
  acknowledged_by: 'StateHealthAdmin',
})
const resolved = alert({
  id: 40,
  state: 'RESOLVED',
  message: 'Delivery worker ords_delivery reported STOPPED.',
  resolved_at: '2026-10-10T06:00:00Z',
  operator: null,
  details: { resolution: { kind: 'CLEANUP', actor: 'StateHealthAdmin', reason: 'Stranded by the 2.6.27 recovery image.', at: '2026-10-10T06:00:00Z' } },
  resolution: { kind: 'CLEANUP', actor: 'StateHealthAdmin', reason: 'Stranded by the 2.6.27 recovery image.', at: '2026-10-10T06:00:00Z' },
})
const legacy = alert({ id: 30, state: 'ACKNOWLEDGED', code: 'ESP_OFFLINE', plain_title: 'No internet', acknowledged_at: '2026-10-01T07:30:00Z' })

const baseCounts: ProblemCounts = {
  open: 2, seen: 1, legacy_seen: 0, fixed: 4,
  by_division: { life: { open: 1, seen: 0, fixed: 0 }, health: { open: 1, seen: 1, fixed: 4 } },
  by_severity: { CRITICAL: 0, HIGH: 1, WARNING: 1, MEDIUM: 0, INFO: 0 },
  by_type: { link_box: 1, hr_system: 1 },
  legacy_by_code: {},
}
let counts: ProblemCounts | undefined = baseCounts
let queues: Record<string, ProblemRow[]> = {}
let resolveResponses: Response[] = []
let failAlerts = false

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

let currentLocation = ''
function LocationProbe() {
  const location = useLocation()
  currentLocation = `${location.pathname}${location.search}`
  return null
}

function renderPage(path = '/problems', props: Partial<Parameters<typeof ProblemsView>[0]> = {}) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ToastProvider>
        <Routes>
          <Route path="/problems" element={<><ProblemsView devices={[device, lifeDevice]} revision={0} username="Sara" {...props} /><LocationProbe /></>} />
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>,
  )
}

const alertRequests = () => vi.mocked(fetch).mock.calls
  .map(([input]) => new URL(String(input), 'http://localhost'))
  .filter((url) => url.pathname === '/api/v1/alerts')

describe('Problems page', () => {
  beforeEach(() => {
    window.localStorage.clear()
    reloadPrefsForTests()
    counts = baseCounts
    failAlerts = false
    resolveResponses = []
    queues = { NEEDS_ACTION: [needsAction, workflow], SEEN: [acknowledged], RESOLVED: [resolved], LEGACY_SEEN: [legacy] }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost')
      if (url.pathname === '/api/v1/alerts') {
        if (failAlerts) return json({ detail: 'down' }, 503)
        const body: ProblemsResponse = {
          rows: queues[url.searchParams.get('queue') || 'NEEDS_ACTION'] || [],
          next_cursor: null,
          totals: { all: 6, open: 2, acknowledged: 0, resolved: 4 },
          queue_totals: { needs_action: 2, acknowledged: 1, resolved: 4, all: 7 },
          ...(counts ? { counts } : {}),
        }
        return json(body)
      }
      if (url.pathname === '/api/v1/attendance-quarantine') return json({ totals: { all: 0, open: 0 }, filtered_total: 0, rows: [], next_cursor: null })
      if (/^\/api\/v1\/alerts\/\d+\/resolve$/.test(url.pathname) && init?.method === 'POST') {
        return resolveResponses.shift() || json({ alert: { ...needsAction, state: 'RESOLVED' }, device_state: 'ONLINE', device_error: null, residual_alert_id: null })
      }
      if (/^\/api\/v1\/alerts\/\d+\/acknowledge$/.test(url.pathname) && init?.method === 'POST') {
        return json({ ...needsAction, acknowledged_at: '2026-10-10T08:05:00Z', acknowledged_by: 'Sara' })
      }
      throw new Error(`Unexpected request: ${url.pathname}${url.search}`)
    }))
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    resetPrefs()
    window.localStorage.clear()
  })

  it('shows New / Seen / Fixed with counts, grouped by device with friendly names', async () => {
    const { container } = renderPage()
    expect(await screen.findByRole('heading', { name: 'Link box is stuck sending attendance' })).toBeTruthy()
    expect(alertRequests()[0].searchParams.get('queue')).toBe('NEEDS_ACTION')
    expect(alertRequests()[0].searchParams.has('state')).toBe(false)
    const tablist = screen.getByRole('tablist', { name: 'Problem status' })
    expect(within(tablist).getByRole('tab', { name: 'New 2' }).getAttribute('aria-selected')).toBe('true')
    expect(within(tablist).getByRole('tab', { name: 'Seen 1' })).toBeTruthy()
    expect(within(tablist).getByRole('tab', { name: 'Fixed 4' })).toBeTruthy()
    expect(within(tablist).queryByRole('tab', { name: /All/ })).toBeNull()
    expect(screen.getByText('Nobody has looked at these yet.')).toBeTruthy()
    // One group per device: friendly name, Life / Health tag, worst problem first (Important before Low).
    const groups = screen.getAllByRole('region').filter((element) => element.classList.contains('problem-group'))
    expect(groups.map((group) => group.getAttribute('aria-label'))).toEqual(['Peshawar · Device 2', 'Building 9 · Device 3'])
    expect(within(groups[1]).getByText('Life')).toBeTruthy()
    expect(within(groups[0]).getByText('Health')).toBeTruthy()
    expect(within(groups[0]).getByRole('link', { name: /Open device/ }).getAttribute('href')).toBe('/devices/connector-peshawar-02?tab=problems')
    expect(screen.getByText(/2 new problems at 2 devices/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Life 1' })).toBeTruthy()
    // Codes, the system message and details only inside Technical details.
    const card = screen.getByRole('article', { name: 'Link box is stuck sending attendance' })
    const technical = card.querySelector('[data-technical]') as HTMLElement
    expect(within(technical).getByText(/Evidence: add_delivery STOPPED · Firmware 2\.6\.27 · Inferred on an earlier boot/)).toBeTruthy()
    expect(within(technical).getByText(needsAction.message)).toBeTruthy()
    expect((technical as HTMLDetailsElement).open).toBe(false)
    expect(card.getAttribute('data-tone')).toBe('attention')
    expect(within(card).getByLabelText('How serious: Important')).toBeTruthy()
    expectPlainText(container)
  })

  it('shows Mark as fixed only when the backend allows it, and says how the others close', async () => {
    renderPage()
    const fixable = await screen.findByRole('article', { name: 'Link box is stuck sending attendance' })
    expect(within(fixable).getByRole('button', { name: /Mark as fixed/ })).toBeTruthy()
    const owned = screen.getByRole('article', { name: workflow.plain_title! })
    expect(within(owned).queryByRole('button', { name: /Mark as fixed/ })).toBeNull()
    expect(within(owned).getByText(/^Closes when its task is finished on its own page\. Closes by itself when the HR system accepts the check-ins\.$/)).toBeTruthy()
    expect(within(owned).getByRole('button', { name: 'Mark as seen' })).toBeTruthy()
  })

  it('switches tabs through the URL and shows seen and fixed cards in plain words', async () => {
    renderPage()
    await screen.findByRole('heading', { name: 'Link box is stuck sending attendance' })
    fireEvent.click(screen.getByRole('tab', { name: 'Seen 1' }))
    const card = await screen.findByRole('article', { name: 'Machine clock is 4 min fast' })
    expect(alertRequests().at(-1)?.searchParams.get('queue')).toBe('SEEN')
    expect(currentLocation).toBe('/problems?tab=seen')
    expect(within(card).getByText('Seen')).toBeTruthy()
    expect(within(card).getByText(/^Seen by StateHealthAdmin · .+/)).toBeTruthy()
    expect(within(card).getByText('“Clock reset scheduled”')).toBeTruthy()
    expect(within(card).queryByRole('button', { name: 'Mark as seen' })).toBeNull()
    expect(within(card).getByRole('button', { name: /Mark as fixed/ })).toBeTruthy()

    fireEvent.click(screen.getByRole('tab', { name: 'Fixed 4' }))
    const fixed = await screen.findByRole('article', { name: 'Link box is stuck sending attendance' })
    const request = alertRequests().at(-1)!
    expect(request.searchParams.get('queue')).toBe('RESOLVED')
    expect(request.searchParams.get('order')).toBe('fixed_newest')
    expect(request.searchParams.get('limit')).toBe('50')
    const from = Date.parse(request.searchParams.get('from_time') || '')
    expect(Date.now() - from).toBeGreaterThan(6 * 86_400_000)
    expect(Date.now() - from).toBeLessThan(8 * 86_400_000)
    expect(within(fixed).getByText('Fixed')).toBeTruthy()
    expect(within(fixed).getByText(/^Closed in a clean-up by StateHealthAdmin at/)).toBeTruthy()
    expect(within(fixed).queryByRole('button', { name: /Mark as fixed/ })).toBeNull()
    expect(screen.getByText('Closed in the last 7 days.')).toBeTruthy()
  })

  it('restores filters from the link and sends them to the server', async () => {
    renderPage('/problems?tab=fixed&serious=urgent,important&division=life&when=30d&type=machine&att=1&q=bld9')
    await screen.findByRole('tab', { name: /Fixed/ })
    await waitFor(() => expect(alertRequests().length).toBeGreaterThan(0))
    const request = alertRequests()[0]
    expect(request.searchParams.get('queue')).toBe('RESOLVED')
    expect(request.searchParams.get('severity')).toBe('CRITICAL,HIGH')
    expect(request.searchParams.get('division')).toBe('life')
    expect(request.searchParams.get('type')).toBe('machine')
    expect(request.searchParams.get('affects_attendance')).toBe('true')
    expect(request.searchParams.get('q')).toBe('bld9')
    expect(request.searchParams.get('from_time')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Urgent 0' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: 'Remove filter: Type: Machine' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Remove filter: Last 30 days' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }))
    await waitFor(() => expect(currentLocation).toBe('/problems?tab=fixed&division=life'))
    expect(alertRequests().at(-1)?.searchParams.has('severity')).toBe(false)
  })

  it('maps the old tab name and the Low chip to every low level', async () => {
    renderPage('/problems?tab=open&serious=low')
    await screen.findByRole('tab', { name: /New/ })
    await waitFor(() => expect(alertRequests().length).toBeGreaterThan(0))
    expect(alertRequests()[0].searchParams.get('queue')).toBe('NEEDS_ACTION')
    expect(alertRequests()[0].searchParams.get('severity')).toBe('WARNING,MEDIUM,INFO')
  })

  it('marks one problem as seen: only that button is busy, the card leaves New and counts tick', async () => {
    renderPage()
    const card = await screen.findByRole('article', { name: 'Link box is stuck sending attendance' })
    const other = screen.getByRole('article', { name: workflow.plain_title! })
    queues.NEEDS_ACTION = [workflow]
    counts = { ...baseCounts, open: 1, seen: 2 }
    fireEvent.click(within(card).getByRole('button', { name: 'Mark as seen' }))
    expect(within(card).getByRole('button', { name: /Marking…/ })).toBeTruthy()
    expect((within(other).getByRole('button', { name: 'Mark as seen' }) as HTMLButtonElement).disabled).toBe(false)
    expect(await screen.findByText('Marked as seen. It stays open until it is fixed.')).toBeTruthy()
    const post = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith('/api/v1/alerts/41/acknowledge'))
    expect(post?.[1]?.method).toBe('POST')
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({})
    await waitFor(() => expect(screen.queryByRole('article', { name: 'Link box is stuck sending attendance' })).toBeNull())
    expect(screen.getByRole('tab', { name: 'New 1' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Seen 2' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'View' }))
    await waitFor(() => expect(currentLocation).toBe('/problems?tab=seen'))
  })

  it('keeps the reason and the password to mark as fixed, explains a refusal and keeps one request key', async () => {
    resolveResponses = [json({
      detail: {
        code: 'ALERT_CONDITION_CURRENT',
        message: 'The latest evidence still asserts this condition.',
        clear_condition: 'Clears when every required delivery worker reports running with a fresh tick.',
      },
    }, 409)]
    renderPage()
    const card = await screen.findByRole('article', { name: 'Link box is stuck sending attendance' })
    fireEvent.click(within(card).getByRole('button', { name: /Mark as fixed/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Mark this problem as fixed' })
    const submit = within(dialog).getByRole('button', { name: 'Mark as fixed' }) as HTMLButtonElement
    const reason = within(dialog).getByLabelText('What did you do?')
    const password = within(dialog).getByLabelText('Enter your password to confirm') as HTMLInputElement
    expect(submit.disabled).toBe(true)
    fireEvent.change(reason, { target: { value: 'Too short' } })
    fireEvent.change(password, { target: { value: 'local-step-up' } })
    expect(submit.disabled).toBe(true)
    expect(within(dialog).getByText('1 more letter needed')).toBeTruthy()
    fireEvent.change(reason, { target: { value: 'Raised under the 2.6.27 recovery image; 2.5.2 cannot report workers.' } })
    expect(within(dialog).queryByText(/more letter/)).toBeNull()
    expect(submit.disabled).toBe(false)
    fireEvent.click(submit)

    expect(await within(dialog).findByText(/The device still has this problem, so it can't be closed by hand yet/)).toBeTruthy()
    expect(within(dialog).getByText('Closes by itself when sending works again.')).toBeTruthy()
    const technical = dialog.querySelector('.mark-fixed-error [data-technical]') as HTMLElement
    expect(within(technical).getByText('The latest evidence still asserts this condition.')).toBeTruthy()
    expect(password.value).toBe('')
    expect(submit.disabled).toBe(true)

    fireEvent.change(password, { target: { value: 'local-step-up' } })
    fireEvent.click(submit)
    expect(await screen.findByText(/^Marked as fixed\./)).toBeTruthy()
    expect(screen.queryByRole('dialog', { name: 'Mark this problem as fixed' })).toBeNull()
    const posts = vi.mocked(fetch).mock.calls.filter(([input, init]) => String(input).endsWith('/api/v1/alerts/41/resolve') && init?.method === 'POST')
    expect(posts).toHaveLength(2)
    const bodies = posts.map(([, init]) => JSON.parse(String(init?.body)))
    expect(bodies[1]).toEqual({
      reason: 'Raised under the 2.6.27 recovery image; 2.5.2 cannot report workers.',
      password: 'local-step-up',
      idempotency_key: expect.stringMatching(/^alert-resolve:/),
    })
    expect(bodies[0].idempotency_key).toBe(bodies[1].idempotency_key)
    expect(await screen.findByText('Marked as fixed')).toBeTruthy()
  })

  it('opens Clean up old problems from the ⋯ menu with Pick devices first', async () => {
    renderPage()
    await screen.findByRole('heading', { name: 'Link box is stuck sending attendance' })
    fireEvent.click(screen.getByRole('button', { name: 'More actions' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Clean up old problems…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Clean up old problems' }, { timeout: 5000 })
    expect((within(dialog).getByRole('radio', { name: /Pick devices/ }) as HTMLInputElement).checked).toBe(true)
    expect(within(dialog).queryByText(/Peshawar 02 \+ 06/)).toBeNull()
  })

  it('shows skeletons and no counts before the first answer, then an error with Try again', async () => {
    failAlerts = true
    renderPage()
    expect(screen.getByText('Loading problems…', { selector: '.problems-skeleton .sr-only' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'New' })).toBeTruthy()
    expect(await screen.findByText("Couldn't load problems.")).toBeTruthy()
    expect(screen.getByText('Check your internet and try again.')).toBeTruthy()
    failAlerts = false
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('heading', { name: 'Link box is stuck sending attendance' })).toBeTruthy()
  })

  it('says when nothing matches the filters and when everything is clear', async () => {
    queues.NEEDS_ACTION = []
    counts = { ...baseCounts, open: 0, seen: 0 }
    renderPage('/problems?q=nothing')
    expect(await screen.findByText('Nothing matches these filters.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(await screen.findByText('All clear.')).toBeTruthy()
  })

  it('hides the old seen marks behind one note and lists them on request', async () => {
    counts = { ...baseCounts, legacy_seen: 3, legacy_by_code: { ESP_OFFLINE: 3 } }
    renderPage('/problems?tab=seen')
    expect(await screen.findByText('3 old “seen” marks from before 10 Oct are hidden.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Show them' }))
    expect(await screen.findByRole('article', { name: 'No internet' })).toBeTruthy()
    expect(alertRequests().at(-1)?.searchParams.get('queue')).toBe('LEGACY_SEEN')
    expect(currentLocation).toBe('/problems?tab=seen&old=1')
  })

  it('falls back to the older totals and the group title on an older backend', async () => {
    counts = undefined
    queues.NEEDS_ACTION = [alert({ plain_title: null, plain_detail: null, next_step: null, operator: undefined, device: { connector_id: device.connector_id, display_name: device.display_name, zone_id: device.zone_id, hardware_id: device.hardware_id } })]
    const { container } = renderPage()
    expect(await screen.findByRole('heading', { name: 'Link box is stuck sending attendance' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'New 2' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Seen 1' })).toBeTruthy()
    const card = screen.getByRole('article', { name: 'Link box is stuck sending attendance' })
    expect(within(card).getByRole('button', { name: /Mark as fixed/ })).toBeTruthy()
    expect(screen.getAllByText('Peshawar · Device 2').length).toBeGreaterThan(0)
    expectPlainText(container)
  })
})

describe('groupByDevice', () => {
  it('puts the worst device first, then the earliest start', () => {
    const groups = groupByDevice([
      alert({ id: 1, severity: 'WARNING', device: ref(lifeDevice), first_seen_at: '2026-10-01T00:00:00Z' }),
      alert({ id: 2, severity: 'CRITICAL', device: ref(device), first_seen_at: '2026-10-05T00:00:00Z' }),
      alert({ id: 3, severity: 'INFO', device: ref(device), first_seen_at: '2026-10-02T00:00:00Z' }),
    ])
    expect(groups.map((group) => group.id)).toEqual(['connector-peshawar-02', 'connector-bld9-03'])
    expect(groups[0].rows.map((row) => row.id)).toEqual([2, 3])
    expect(groups[0].since).toBe('2026-10-02T00:00:00Z')
  })
})
