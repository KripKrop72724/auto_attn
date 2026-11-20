import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DeviceDetail } from '../../words/device'
import { expectPlainText } from '../../words/testing'
import { problemWords } from '../../words/problems'
import {
  alertRow, bodyOf, calls, commKeyState, currentUrl, device, durabilityReason, fullHealth, json, renderDevice,
  resetDeviceTests, stubFetch, workerReason, zkt, type Handler,
} from './testHarness'

let detail: DeviceDetail = device
let active = [] as ReturnType<typeof alertRow>[]
let resolved = [] as ReturnType<typeof alertRow>[]

const today = {
  day: new Date().toISOString().slice(0, 10),
  last_checkin_at: new Date(Date.now() - 120_000).toISOString(),
  checkins_today: 214,
  sent_to_hr_today: 198,
  waiting_to_send: 16,
  not_matched_today: 3,
  computed_at: new Date().toISOString(),
}

const base: Handler = (path, init) => {
  if (path.includes('/alerts?queue=ACTIVE')) return json({ rows: active })
  if (path.includes('/alerts?queue=RESOLVED')) return json({ rows: resolved })
  if (/\/api\/v1\/alerts\/\d+\/acknowledge$/.test(path) && init?.method === 'POST') {
    active = active.map((row) => ({ ...row, acknowledged_at: new Date().toISOString(), acknowledged_by: 'StateHealthAdmin' }))
    return json(active[0])
  }
  if (path.includes('/activity?')) return json({ items: [], next_cursor: null, newest_id: null, counts: { problems: 0, retried: 0, routine: 0, by_topic: {} } })
  if (path.includes('/connectivity?')) return json({ rows: [] })
  if (path.endsWith('/comm-key')) return json(commKeyState)
  if (path.endsWith('/reconciliations/preflight')) return json({ eligible: true, ready_now: true, hard_blockers: [], waitable_blockers: [], terminal: null, coverage: null })
  if (path.endsWith(`/devices/${device.connector_id}`)) return json(detail)
  return undefined
}

beforeEach(() => {
  resetDeviceTests()
  detail = { ...device, attendance_today: today }
  active = []
  resolved = []
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('device header and frame', () => {
  it('names the device by its friendly name, with division, status and one plain sentence (no MAC)', async () => {
    stubFetch(base)
    renderDevice()
    const dialog = await screen.findByRole('dialog', { name: 'Quetta' })
    const description = document.getElementById(dialog.getAttribute('aria-describedby') || '')
    expect(description?.textContent).toMatch(/^Working/)
    expect(description?.textContent).not.toContain('00:17:61')
    expect(within(dialog).getAllByText('Health').length).toBeGreaterThan(0)
    expect(within(dialog).getByLabelText('Status: Working')).toBeTruthy()
    expect(within(dialog).getByText(/^Last heard from /)).toBeTruthy()
    expect(within(dialog).getAllByRole('tab').map((tab) => tab.textContent?.trim())).toEqual(['Summary', 'Activity', 'Problems', 'People', 'Tools'])
    expect(within(dialog).getByRole('tablist', { name: 'Device sections' })).toBeTruthy()
    await screen.findByText('198 of 214')
    expectPlainText(dialog)
  })

  it('says what is wrong and since when for a device that needs attention', async () => {
    detail = { ...detail, state: 'DEGRADED', health: fullHealth() }
    stubFetch(base)
    renderDevice({ ...device, state: 'DEGRADED', health: fullHealth() })
    const dialog = await screen.findByRole('dialog', { name: 'Quetta' })
    const title = problemWords({ code: 'ESP_DURABILITY_FAULT' }).title
    expect(within(dialog).getByText(new RegExp(`^${title} since `))).toBeTruthy()
    expect(within(dialog).getByLabelText('Status: Needs attention')).toBeTruthy()
    expect((await within(dialog).findAllByRole('heading', { name: title })).length).toBeGreaterThan(0)
    expect(within(dialog).getByText(/^What to do: /)).toBeTruthy()
    expectPlainText(dialog)
  })

  it('treats a machine that is not responding as Offline, like /status (D-1)', async () => {
    const down = { ...device, terminal_link: { state: 'DISCONNECTED', raw_state: 'RETRY_WAIT', since: new Date(Date.now() - 3_600_000).toISOString(), reason: 'HIK_NETWORK', message: null } }
    detail = { ...detail, ...down, offline_reason: 'machine_not_responding' }
    stubFetch(base)
    renderDevice(down)
    const dialog = await screen.findByRole('dialog', { name: 'Quetta' })
    expect(within(dialog).getByLabelText('Status: Offline')).toBeTruthy()
    expect(within(dialog).getByText(/^Machine not responding since .*The attendance machine may be switched off or its cable unplugged\.$/)).toBeTruthy()
  })

  it('moves between tabs with the keyboard, keeps the tab in the URL and closes on Escape', async () => {
    stubFetch(base)
    const { onClose } = renderDevice()
    const summary = await screen.findByRole('tab', { name: 'Summary' })
    expect(summary.id).toBe('device-tab-summary')
    summary.focus()
    fireEvent.keyDown(summary, { key: 'ArrowRight' })
    const activity = screen.getByRole('tab', { name: 'Activity' })
    await waitFor(() => expect(activity.getAttribute('aria-selected')).toBe('true'))
    expect(screen.getByRole('tabpanel').getAttribute('aria-labelledby')).toBe('device-tab-activity')
    expect(currentUrl()).toBe('/devices/connector-quetta?tab=activity')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('reloads only when this device changes, at most every 3 s', async () => {
    stubFetch(base)
    const { rerenderRevision } = renderDevice()
    await screen.findByText('198 of 214')
    const before = calls(/connector-quetta$/).length
    rerenderRevision(1)
    await waitFor(() => expect(calls(/connector-quetta$/).length).toBe(before + 1))
  })
})

describe('Summary', () => {
  it('shows placeholders, never zero, until today’s numbers arrive', async () => {
    let release: (value: Response) => void = () => undefined
    stubFetch((path) => (path.endsWith(`/devices/${device.connector_id}`) ? new Promise<Response>((resolve) => { release = resolve }) : undefined), base)
    renderDevice()
    const facts = await screen.findByText('Check-ins today')
    expect(facts.closest('div')?.textContent).toBe('Check-ins today')
    const factList = facts.closest('dl') as HTMLElement
    expect(within(factList).queryByText('0')).toBeNull()
    await act(async () => release(json(detail)))
    expect(await screen.findByText('214')).toBeTruthy()
    expect(screen.getByText('198 of 214')).toBeTruthy()
    expect(screen.getByRole('link', { name: '16 waiting' })).toBeTruthy()
    expect(screen.getByRole('link', { name: '3 not matched to an employee' })).toBeTruthy()
    expect(screen.getByText(/^Today = since midnight, Pakistan time · Updated /)).toBeTruthy()
  })

  it('keeps codes, MAC and serials in a closed Technical details box', async () => {
    detail = { ...detail, zkt: zkt() }
    stubFetch(base)
    renderDevice({ ...device, zkt: zkt() })
    const summary = await screen.findByText('Technical details')
    const box = summary.closest('details') as HTMLDetailsElement
    expect(box.open).toBe(false)
    expect(box.hasAttribute('data-technical')).toBe(true)
    expect(within(box).getByText('00:17:61:12:03:32')).toBeTruthy()
    expect(within(box).getByText('QUETTA-01')).toBeTruthy()
    expect(screen.getAllByText('uFace800').length).toBeGreaterThan(0)
    expect(screen.getByText('Correct')).toBeTruthy()
  })

  it('shows the Hikvision record checks inside Technical details', async () => {
    const hik = { ...device, firmware_family: 'hikvision' as const, hikvision: { capture_mode: 'poll' as const, poll_interval_seconds: 2, last_poll_interval_ms: 2150, qualification_state: 'NOT_QUALIFIED', poll_error: 0, source_queue_depth: 0 } }
    detail = { ...detail, ...hik }
    stubFetch(base)
    renderDevice(hik)
    const box = (await screen.findByText('Technical details')).closest('details') as HTMLElement
    expect(within(box).getByText('Checks the machine every 2 seconds')).toBeTruthy()
    expect(within(box).getByText('2.1 seconds')).toBeTruthy()
    expect(within(box).getByText('New check-ins keep arriving while older records are checked. The full history is in Recover attendance.')).toBeTruthy()
    expect(screen.getAllByText('Hikvision').length).toBeGreaterThan(0)
    expectPlainText(screen.getByRole('dialog'))
  })

  it('points a different machine at Advanced tools', async () => {
    const swapped = { ...device, zkt: zkt({ serial: 'CKPG221260408' }) }
    detail = { ...detail, ...swapped }
    stubFetch(base)
    renderDevice(swapped)
    fireEvent.click(await screen.findByRole('button', { name: 'Open Advanced tools' }))
    await waitFor(() => expect(currentUrl()).toContain('tab=tools'))
    expect(currentUrl()).toContain('advanced=1')
    expect(await screen.findByRole('heading', { name: 'A different machine is connected' }, { timeout: 8000 })).toBeTruthy()
  })

  it('says when the detail could not load, with Try again', async () => {
    stubFetch((path) => (path.endsWith(`/devices/${device.connector_id}`) ? json({ detail: 'boom' }, 503) : undefined), base)
    renderDevice()
    expect(await screen.findByText("Couldn't load this device's details.")).toBeTruthy()
    expect(screen.getAllByRole('button', { name: 'Try again' }).length).toBeGreaterThan(0)
    expect(screen.getByText(/^Status not confirmed\./)).toBeTruthy()
  })
})

describe('Problems', () => {
  it('lists happening now, new and seen problems, marks one as seen on that card only, and keeps the panel open under the fix dialog', async () => {
    const derived = { ...workerReason, code: 'ZKT_LINK_DOWN', alert_id: null }
    detail = { ...detail, state: 'DEGRADED', health: fullHealth({ reasons: [durabilityReason, derived] }) }
    active = [
      alertRow({}),
      alertRow({ id: 43, code: 'ZKT_CLOCK_DRIFT', message: 'Terminal clock drift exceeds two minutes.', acknowledged_at: '2026-10-10T07:30:00Z', acknowledged_by: 'StateHealthAdmin', details: { acknowledgement_note: 'Clock reset scheduled' } }),
    ]
    resolved = [alertRow({ id: 40, state: 'RESOLVED', resolved_at: '2026-10-10T06:00:00Z', resolution: { kind: 'OPERATOR', actor: 'StateHealthAdmin', reason: 'Stranded by the recovery image.', at: '2026-10-10T06:00:00Z' } })]
    stubFetch(base)
    const { onClose } = renderDevice({ ...device, state: 'DEGRADED', health: fullHealth({ reasons: [durabilityReason, derived] }) }, { search: '?tab=problems' })
    const tab = await screen.findByRole('tab', { name: /^Problems/ })
    await waitFor(() => expect(tab.textContent).toContain('1'))
    expect(calls(`/devices/${device.connector_id}/alerts?queue=ACTIVE&limit=200`).length).toBeGreaterThan(0)
    await waitFor(() => expect(calls(`/devices/${device.connector_id}/alerts?queue=RESOLVED&limit=20`).length).toBeGreaterThan(0))

    expect(await screen.findByRole('heading', { name: 'Happening now' })).toBeTruthy()
    const open = screen.getByRole('heading', { name: 'Not fixed yet (2)' }).closest('section') as HTMLElement
    const cards = within(open).getAllByRole('article')
    expect(within(cards[0]).getByText('New')).toBeTruthy()
    expect(within(cards[1]).getByText('Seen')).toBeTruthy()
    expect(within(cards[1]).getByText(/^Seen by StateHealthAdmin at /).closest('p')?.textContent).toMatch(/^Seen by StateHealthAdmin at .* Still open until it is fixed\. Note: Clock reset scheduled$/)
    expect(within(cards[1]).queryByRole('button', { name: 'Mark as seen' })).toBeNull()
    expect(await screen.findByText('Fixed recently (1)')).toBeTruthy()

    fireEvent.click(within(cards[0]).getByRole('button', { name: 'Mark as fixed…' }))
    await screen.findByRole('dialog', { name: /Resolve|fixed/i })
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /Resolve|fixed/i })).toBeNull())
    expect(onClose).not.toHaveBeenCalled()

    fireEvent.click(within(cards[0]).getByRole('button', { name: 'Mark as seen' }))
    expect(await screen.findByText('Marked as seen. It stays open until the problem is fixed.')).toBeTruthy()
    expect(calls(/\/api\/v1\/alerts\/41\/acknowledge$/)).toHaveLength(1)
    expectPlainText(screen.getByRole('tabpanel'))
  })

  it('hides Mark as fixed when the device still reports the problem, with the plain reason', async () => {
    detail = { ...detail, state: 'DEGRADED', health: fullHealth() }
    active = [alertRow({ id: 42, code: 'ESP_DURABILITY_FAULT', message: 'Storage durability is DEGRADED.' })]
    stubFetch(base)
    renderDevice({ ...device, state: 'DEGRADED', health: fullHealth() }, { search: '?tab=problems' })
    const card = await screen.findByRole('article', { name: problemWords({ code: 'ESP_DURABILITY_FAULT' }).title })
    expect(within(card).queryByRole('button', { name: 'Mark as fixed…' })).toBeNull()
    expect(within(card).getByText(/^Waiting for the link box|Still happening|Closes/)).toBeTruthy()
  })

  it('shows one empty state when nothing is open', async () => {
    stubFetch(base)
    renderDevice(device, { search: '?tab=problems' })
    expect(await screen.findByText('No problems on this device.')).toBeTruthy()
  })
})

describe('People', () => {
  it('shows the count, when the list was read and whether changes are allowed', async () => {
    detail = { ...detail, zkt: zkt() }
    stubFetch(base)
    const { onManageUsers } = renderDevice({ ...device, zkt: zkt() }, { search: '?tab=people' })
    expect(await screen.findByText('189')).toBeTruthy()
    expect(screen.getByText(/^List last read from the machine 10 min ago/)).toBeTruthy()
    expect(screen.getByText('Changes to people: allowed')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Open people on this machine' }))
    expect(onManageUsers).toHaveBeenCalledTimes(1)
    expectPlainText(screen.getByRole('tabpanel'))
  })
})

describe('Tools', () => {
  it('refreshes the people list once with an idempotency key and follows it to Confirmed', async () => {
    let status = 'QUEUED'
    detail = { ...detail, zkt: zkt() }
    stubFetch((path, init) => {
      if (path.endsWith('/users/refresh') && init?.method === 'POST') return json({ command_id: 'cmd-1', type: 'REFRESH_USERS', status: 'QUEUED', created_at: new Date().toISOString(), expires_at: null })
      if (path.endsWith('/api/v2/commands/cmd-1')) return json({ command_id: 'cmd-1', type: 'REFRESH_USERS', status, created_at: new Date().toISOString(), expires_at: null })
      return undefined
    }, base)
    renderDevice({ ...device, zkt: zkt() }, { search: '?tab=tools' })
    const card = (await screen.findByRole('heading', { name: 'Refresh people list from the machine' })).closest('section') as HTMLElement
    const button = within(card).getByRole('button', { name: 'Refresh now' })
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(calls('/users/refresh')).toHaveLength(1))
    expect(bodyOf('/users/refresh')).toEqual({ idempotency_key: expect.stringMatching(/^refresh-people:/) })
    await waitFor(() => expect((within(card).getByRole('button', { name: 'Refresh now' }) as HTMLButtonElement).disabled).toBe(true))
    expect(within(card).getByText('Waiting')).toBeTruthy()
    status = 'SUCCEEDED'
    expect(await within(card).findByText('People list refreshed: 189 people.', {}, { timeout: 5000 })).toBeTruthy()
  })

  it('shows why a refresh did not work', async () => {
    stubFetch((path, init) => {
      if (path.endsWith('/users/refresh') && init?.method === 'POST') return json({ command_id: 'cmd-2', type: 'REFRESH_USERS', status: 'QUEUED', created_at: new Date().toISOString(), expires_at: null })
      if (path.endsWith('/api/v2/commands/cmd-2')) return json({ command_id: 'cmd-2', type: 'REFRESH_USERS', status: 'FAILED', error_code: 'COMMAND_EXECUTOR_BUSY', created_at: new Date().toISOString(), expires_at: null })
      return undefined
    }, base)
    renderDevice(device, { search: '?tab=tools' })
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh now' }))
    expect(await screen.findByText("Didn't work: The link box is busy; try again in a minute.", {}, { timeout: 5000 })).toBeTruthy()
  })

  it('starts Recover attendance in one click with the same body as today', async () => {
    stubFetch((path, init) => {
      if (path.endsWith('/reconciliations/full-history') && init?.method === 'POST') return json({ job_id: 'job-1', status: 'QUEUED' })
      if (path.endsWith('/api/v1/reconciliations/job-1')) return json({ job_id: 'job-1', status: 'QUEUED' })
      return undefined
    }, base)
    renderDevice(device, { search: '?tab=tools' })
    expect(await screen.findByText('Ready to start.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(await screen.findByText(/^Started at /)).toBeTruthy()
    expect(bodyOf('/reconciliations/full-history')).toEqual({ idempotency_key: expect.stringMatching(/^full-history:/), scope: 'ALL_RECORDS' })
  })

  it('says why a recovery cannot start yet', async () => {
    stubFetch((path) => (path.endsWith('/reconciliations/preflight') ? json({ eligible: true, ready_now: false, hard_blockers: [], waitable_blockers: [{ code: 'WAITING_FOR_DEVICE', message: 'The ESP connector is offline.' }], terminal: null, coverage: null }) : undefined), base)
    renderDevice(device, { search: '?tab=tools' })
    expect(await screen.findByText('It will be able to start when the link box is back online.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Start' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('hides restart for machines that cannot restart and blocks it during temporary admin access', async () => {
    detail = { ...detail, zkt: zkt({ capabilities: {} }) }
    stubFetch(base)
    renderDevice({ ...device, zkt: zkt({ capabilities: {} }) }, { search: '?tab=tools' })
    expect(await screen.findByText("This machine model can't be restarted from here.")).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Restart machine…' })).toBeNull()
    cleanup()
    resetDeviceTests()
    const leased = { ...device, zkt: zkt(), active_lease: { lease_id: 'l1', state: 'ACTIVE', requested_at: '', granted_at: null, expires_at: new Date(Date.now() + 600_000).toISOString(), revoked_at: null, last_error: null } }
    detail = { ...detail, ...leased }
    renderDevice(leased, { search: '?tab=tools' })
    expect(await screen.findByText(/^Can't restart while someone has temporary admin access/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Restart machine…' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('keeps the reason and password for a restart, and clears a wrong password', async () => {
    detail = { ...detail, zkt: zkt() }
    stubFetch((path, init) => (path.endsWith('/restart') && init?.method === 'POST' ? json({ detail: 'Password confirmation failed.' }, 403) : undefined), base)
    renderDevice({ ...device, zkt: zkt() }, { search: '?tab=tools' })
    fireEvent.click(await screen.findByRole('button', { name: 'Restart machine…' }))
    const sheet = await screen.findByRole('dialog', { name: 'Restart the machine at Quetta?' })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Restart machine' }))
    expect(await within(sheet).findByText('Enter your password.')).toBeTruthy()
    expect((within(sheet).getByLabelText('Reason') as HTMLInputElement).value).toBe('Restarted from the dashboard')
    fireEvent.change(within(sheet).getByLabelText('Your password'), { target: { value: 'wrong' } })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Restart machine' }))
    expect(await within(sheet).findByText('That password is not right. Type it again.')).toBeTruthy()
    expect((within(sheet).getByLabelText('Your password') as HTMLInputElement).value).toBe('')
    expect(bodyOf('/restart')).toEqual({ reason: 'Restarted from the dashboard', password: 'wrong', idempotency_key: expect.stringMatching(/^restart:/) })
  })
})

describe('Advanced tools', () => {
  const openAdvanced = async () => {
    fireEvent.click(await screen.findByRole('button', { name: 'Advanced tools · for IT' }))
  }

  it('keeps the reveal safeguards and hides the machine password on blur', async () => {
    stubFetch((path, init) => (path.endsWith('/comm-key/reveal') && init?.method === 'POST'
      ? json({ comm_key: '1979', applied_revision: 1, verified_terminal_serial: 'UFS2253100068', last_verified_at: new Date().toISOString() })
      : undefined), base)
    renderDevice(device, { search: '?tab=tools' })
    await openAdvanced()
    const card = (await screen.findByRole('heading', { name: 'Show the machine password' }, { timeout: 8000 })).closest('section') as HTMLElement
    fireEvent.change(within(card).getByLabelText('Why do you need it?'), { target: { value: 'Authorized Quetta recovery credential inspection' } })
    fireEvent.change(within(card).getByLabelText(/Type REVEAL connector-quetta to confirm/), { target: { value: 'REVEAL connector-quetta' } })
    fireEvent.change(within(card).getByLabelText('Your password'), { target: { value: 'correct-password' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Show for 15 seconds' }))
    expect(await screen.findByText('1979')).not.toBeNull()
    expect(screen.getByText(/^Hides in \d+ s$/)).toBeTruthy()
    expect(bodyOf('/comm-key/reveal')).toEqual({ password: 'correct-password', reason: 'Authorized Quetta recovery credential inspection', typed_confirmation: 'REVEAL connector-quetta' })
    fireEvent.blur(window)
    await waitFor(() => expect(screen.queryByText('1979')).toBeNull())
  })

  it('confirms a new machine with the exact old and new serials', async () => {
    const swapped = { ...device, state: 'DEGRADED' as const, zkt: zkt({ serial: 'CKPG221260408', writes_disabled_reason: 'SERIAL_MISMATCH' }) }
    detail = { ...detail, ...swapped }
    stubFetch((path, init) => (path.endsWith('/terminal-binding/replace') && init?.method === 'POST' ? json({ device: swapped, command: { command_id: 'replacement-command' } }) : undefined), base)
    renderDevice(swapped, { search: '?tab=tools' })
    const card = (await screen.findByRole('heading', { name: 'A different machine is connected' }, { timeout: 8000 })).closest('section') as HTMLElement
    const submit = within(card).getByRole('button', { name: 'Confirm new machine' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    expect((within(card).getByLabelText('Why? (saved with your name)') as HTMLInputElement).value).toBe('Machine replaced on site')
    fireEvent.change(within(card).getByLabelText(/Type REPLACE connector-quetta/), { target: { value: 'REPLACE connector-quetta AEH2232460004 CKPG221260408' } })
    fireEvent.change(within(card).getByLabelText('Your password'), { target: { value: 'correct-password' } })
    fireEvent.click(submit)
    expect(await within(card).findByText('New machine confirmed. Waiting for the link box to accept it.')).toBeTruthy()
    expect(bodyOf('/terminal-binding/replace')).toMatchObject({
      current_serial: 'AEH2232460004',
      observed_serial: 'CKPG221260408',
      typed_confirmation: 'REPLACE connector-quetta AEH2232460004 CKPG221260408',
      password: 'correct-password',
      idempotency_key: expect.stringMatching(/^terminal-replacement:/),
    })
  })

  it('moves a device to spare and back with Undo', async () => {
    stubFetch((path, init) => {
      if (path.endsWith('/spare') && init?.method === 'PATCH') {
        detail = { ...detail, is_spare: JSON.parse(String(init.body)).spare }
        return json(detail)
      }
      return undefined
    }, base)
    const { onInventoryChanged } = renderDevice(device, { search: '?tab=tools' })
    await openAdvanced()
    expect(await screen.findByText('This device is counted in totals and problems.', {}, { timeout: 8000 })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Use as a spare' }))
    expect(await screen.findByText('Quetta moved to spare devices.')).toBeTruthy()
    expect((await screen.findAllByText(/not counted in totals or problems/i)).length).toBeGreaterThan(0)
    expect(onInventoryChanged).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(bodyOf('/spare')).toEqual({ spare: false }))
    expect(await screen.findByText('Quetta is back in use.')).toBeTruthy()
    expect(onInventoryChanged).toHaveBeenCalledTimes(2)
  })

  it('offers to clear an out-of-date saved error with the existing dialog', async () => {
    const health = fullHealth({ tier: 'ONLINE', reasons: [], primary: null, degraded_count: 0, warning_count: 0, device_error: { code: 'ZKT_CONNECTION_FLAPPING', message: 'Terminal connection is flapping.', derived_code: null, backed: false, backing_alert_ids: [] } })
    detail = { ...detail, health }
    stubFetch(base)
    renderDevice({ ...device, health }, { search: '?tab=tools&advanced=1' })
    fireEvent.click(await screen.findByRole('button', { name: 'Clear an out-of-date error' }, { timeout: 8000 }))
    const dialog = await screen.findByRole('dialog', { name: /device error|out-of-date/i })
    expect(within(dialog).getByRole('button', { name: /Re-evaluate|Clear/ })).toBeTruthy()
  })

  it('opens the engineer custody view only on request', async () => {
    stubFetch((path) => (path.includes('/zkt-custody') ? json({ connector_id: device.connector_id, enabled: false, sampled_at: new Date().toISOString(), counts: [], rows: [], next_cursor: null }) : undefined), base)
    renderDevice(device, { search: '?tab=tools&advanced=1' })
    fireEvent.click(await screen.findByRole('button', { name: 'Show the engineer view' }, { timeout: 8000 }))
    expect(await screen.findByRole('article', { name: 'ZKT ADD custody' })).toBeTruthy()
  })

  it('shows no system words in Tools outside technical details', async () => {
    detail = { ...detail, zkt: zkt() }
    stubFetch(base)
    renderDevice({ ...device, zkt: zkt() }, { search: '?tab=tools&advanced=1' })
    await screen.findByRole('heading', { name: 'Machine password (COMM Key)' }, { timeout: 8000 })
    await screen.findByText('Ready to start.')
    expectPlainText(screen.getByRole('tabpanel'))
  })
})
