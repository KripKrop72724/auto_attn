import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useToast } from '../App'
import type { Device, DeviceUser } from '../types'
import { expectPlainText } from '../words/testing'
import { UsersView } from './UsersWorkspace'
import { UserOperationDialog } from './Users'

const device: Device = {
  connector_id: 'connector-one', hardware_id: 'hw-one', zone_id: 'ZONE-ONE', zone_name: 'Islamabad',
  device_id: '1', display_name: 'SLICTOWER · 3rd Floor', state: 'ONLINE', connected: true,
  firmware_version: '2.4.5', onboarding_generation: 2, last_onboarded_at: null,
  last_seen_at: '2026-08-12T10:00:00Z', current_activity: 'Live capture', last_error_code: null,
  friendly_name: 'SLIC Tower · 3rd Floor', division: 'health',
  zkt: {
    id: 1, serial: 'ADZV211860253', expected_serial: 'ADZV211860253', ip_address: '192.168.1.10',
    model: 'MB20', platform: 'ZLM60', online: true, connection_state: 'ONLINE', consecutive_failures: 0,
    consecutive_successes: 3, flap_count_15m: 0, last_transition_at: null, last_online_at: null,
    offline_since: null, stability_since: null, backoff_until: null, probe_latency_ms: 9,
    certification_state: 'CERTIFIED', certification_observations: 2,
    capabilities: { create_user: true, user_write: true, admin_lease: true, delete_user: true },
    snapshot_complete: true, writes_disabled_reason: null, user_count: 2, attendance_count: 10,
    device_time: null, device_time_sampled_at: null, drift_seconds: 0, last_reconcile_at: null, next_restart_at: null,
  },
}

const user = (overrides: Partial<DeviceUser> & Record<string, unknown> = {}): DeviceUser => ({
  id: 1, user_key: 'user-one', uid: '7', user_id: '1007', display_name: 'Ayesha Khan',
  cnic_masked: '*****-****567-1', cnic_available: true, identity_complete: true,
  identity_conflict_code: null, identity_conflict_members: [], identity_conflict_resolved: false,
  identity_resolution_id: null, shift_worker: false, privilege: 0, present: true, lifecycle_state: 'ACTIVE',
  row_version: 3, observed_at: '2026-08-12T08:00:00Z', machine_name_preview: 'Ayesha-*****-****567-1',
  current_command_state: null, read_only: false, ...overrides,
} as DeviceUser)

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

const command = {
  command_id: 'command-one', type: 'REFRESH_USERS', status: 'QUEUED', created_at: '2026-08-12T10:00:00Z', expires_at: null,
}

const integrity = (overrides: Record<string, number> = {}) => ({
  source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT', total_users: 2, with_cnic: 1, missing_cnic: 1,
  duplicate_groups: 0, duplicate_users: 0, resolved_duplicate_groups: 0,
  unresolved_duplicate_groups: 0, unresolved_duplicate_users: 0, ...overrides,
})

const params = (input: RequestInfo | URL) => new URL(String(input), 'https://add.test').searchParams

function workspaceFetch({
  rows = [user()],
  selectedDevice = device,
  nextOffset = null as number | null,
  failIdentity = false,
  pageRows = [] as DeviceUser[],
  counts = integrity(),
} = {}) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path.includes('/users/refresh') && init?.method === 'POST') return response(command)
    if (path.includes('/api/v2/commands/')) return response(command)
    if (path.endsWith('/api/v1/devices/connector-one')) return response(selectedDevice)
    if (path.includes('/identity-conflicts')) {
      if (failIdentity) return response({ detail: 'Identity evidence is temporarily unavailable.' }, 503)
      return response({ unresolved_groups: 0, resolved_groups: 0, raw_duplicate_groups: 0, groups: [] })
    }
    if (path.includes('/historical-identities')) return response({
      totals: { unresolved_events: 0, blocked_identity: 0, quarantined_identity_reuse: 0, attributed_to_deleted_users: 0, unassigned_events: 0, actionable_event_groups: 0, candidate_users: 0 },
      rows: [], unassigned_groups: [],
    })
    if (path.includes('/user-deletion-jobs/latest')) return response({ job: null })
    if (path.includes('/api/v2/people/summary')) return response({ rows: [] })
    if (path.includes('/api/v2/devices/connector-one/users')) {
      const isPage = Number(params(input).get('offset') || 0) > 0
      const limit = Number(params(input).get('limit') || 200)
      const shown = isPage ? pageRows : limit > rows.length ? [...rows, ...pageRows].slice(0, limit) : rows
      return response({
        rows: shown,
        next_cursor: null,
        next_offset: isPage ? null : nextOffset,
        total: counts.total_users,
        filtered_total: rows.length + (nextOffset ? pageRows.length : 0),
        device: selectedDevice,
        identity_integrity: counts,
      })
    }
    return response({ rows: [] })
  })
}

function UsersHarness({ selectedDevice = device, revision = 0 }: { selectedDevice?: Device; rows?: DeviceUser[]; revision?: number }) {
  const toast = useToast()
  return (
    <BrowserRouter>
      <UsersView devices={[selectedDevice]} selectedDeviceId={selectedDevice.connector_id} onSelectDevice={() => {}} revision={revision} toast={toast} refreshFleet={async () => {}} />
      {toast.toast && <div role={toast.toast.kind === 'error' ? 'alert' : 'status'} data-testid="toast">{toast.toast.text}</div>}
    </BrowserRouter>
  )
}

function DialogHarness({ state, selectedDevice = device, onFetch }: { state: Parameters<typeof UserOperationDialog>[0]['state']; selectedDevice?: Device; onFetch?: unknown }) {
  void onFetch
  const toast = useToast()
  return <UserOperationDialog state={state} device={selectedDevice} onClose={() => {}} onCommand={() => {}} toast={toast} />
}

describe('People on one machine', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    window.history.replaceState(null, '', '/')
  })

  it.each([true, false])('explains whole-profile removal according to the link box software: %s', (allCredentials) => {
    const hik: Device = { ...device, firmware_family: 'hikvision', zkt: {
      ...device.zkt!, capabilities: { delete_user: true, delete_all_credentials: allCredentials },
    } }
    render(<DialogHarness state={{ mode: 'delete', user: user() }} selectedDevice={hik} />)
    expect(screen.getByText(allCredentials ? /deletes their face, fingerprints, card and PIN/ : /older software: their fingerprints or card may stay/)).toBeTruthy()
    if (allCredentials) expect(screen.getAllByText(/Their past check-ins stay saved/).length).toBeGreaterThan(0)
  })

  it('keeps a confirmed Hikvision machine view only without asking to confirm it again', async () => {
    const hik: Device = { ...device, firmware_family: 'hikvision', zkt: {
      ...device.zkt!, model: null, terminal_binding_state: 'CONFIRMED',
      certification_state: 'READ_ONLY', writes_disabled_reason: 'HIKVISION_WRITE_QUALIFICATION_PENDING',
      capabilities: { read_users: true, user_write: false, create_user: false, delete_user: false, admin_lease: false },
    } }
    vi.stubGlobal('fetch', workspaceFetch({ selectedDevice: hik }))
    render(<UsersHarness selectedDevice={hik} />)
    expect(await screen.findByText(/Confirming the machine again won't change this/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /right machine/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Add person/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Edit /i })).toBeNull()
    expect(document.querySelector('[data-technical]')?.textContent).toContain('Machine confirmed: serial ADZV211860253')
    for (const node of Array.from(document.querySelectorAll('body *'))) {
      if (node.children.length === 0 && /Hikvision/.test(node.textContent || '')) expect(node.closest('[data-technical], [data-brand]')).toBeTruthy()
    }
  })

  it.each([
    { permanent: false, pending: false },
    { permanent: false, pending: true },
    { permanent: true, pending: false },
  ])('says when changes allowed by IT end: permanent $permanent, pending $pending', async ({ permanent, pending }) => {
    const hik: Device = { ...device, firmware_family: 'hikvision', zkt: {
      ...device.zkt!, terminal_binding_state: 'CONFIRMED',
      certification_state: 'PROFILE_EXCEPTION', writes_disabled_reason: null,
      capabilities: { user_write: true, create_user: true, delete_user: true, user_role_write: false, admin_lease: false, write_exception_expires_at: permanent ? null : '2026-10-15T16:00:00Z', write_exception_permanent: permanent, write_exception_pending_firmware: pending },
    } }
    vi.stubGlobal('fetch', workspaceFetch({ selectedDevice: hik }))
    render(<UsersHarness selectedDevice={hik} />)
    const notice = await screen.findByRole('region', { name: 'Allowed by IT for this machine' })
    expect(within(notice).getByText('Changes allowed by IT for this machine')).toBeTruthy()
    if (permanent) {
      expect(notice.textContent).toContain('No end date. Only for this machine.')
      expect(notice.textContent).not.toContain('Allowed until')
    } else {
      expect(notice.textContent).toContain('Allowed until')
    }
    if (pending) expect(notice.textContent).toContain('A link box software update will remove this limit.')
    expect(notice.textContent).toContain("The admin role can't be changed here.")
    expect((screen.getByRole('button', { name: /Add person/i }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('keeps the admin role locked under More options while edits are allowed', () => {
    const hik: Device = { ...device, firmware_family: 'hikvision', zkt: {
      ...device.zkt!, certification_state: 'PROFILE_EXCEPTION',
      capabilities: { user_write: true, user_role_write: false },
    } }
    render(<DialogHarness state={{ mode: 'edit', user: user() }} selectedDevice={hik} />)
    fireEvent.click(screen.getByText('More options'))
    expect((screen.getByLabelText(/Role on the machine/) as HTMLSelectElement).disabled).toBe(true)
  })

  it('keeps a wrongly typed CNIC out of the name when the CNIC is set', async () => {
    const saleem = user({
      user_id: '04048', uid: '55', display_name: 'A.Saleem-A4230111028815', cnic_masked: null,
      cnic_available: false, identity_complete: false, machine_name_preview: 'A.Saleem-A4230111028815',
      cnic_format_suspect: true, cnic_format_suspect_reason: 'LETTER_BEFORE_DIGITS',
      suggested_display_name: 'A.Saleem',
    })
    const fetchMock = vi.fn(async () => response({ command, user: saleem }))
    vi.stubGlobal('fetch', fetchMock)
    render(<DialogHarness state={{ mode: 'edit', user: saleem }} />)
    expect(screen.getAllByRole('note')[0].textContent).toMatch(/typed wrongly in their name/)
    expect((screen.getByLabelText('Full name') as HTMLInputElement).value).toBe('A.Saleem')
    fireEvent.change(screen.getByLabelText(/CNIC \(needed — none saved yet\)/), { target: { value: '4230111028815' } })
    expect(screen.getByText('A.Saleem-4230111028815')).toBeTruthy()
    expect(screen.getByText('13 of 13 digits')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'test-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const body = JSON.parse(String((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].body))
    expect(body.display_name).toBe('A.Saleem')
    expect(body.cnic).toBe('4230111028815')
    expect(body.expected_version).toBe(3)
    expect(String(body.idempotency_key)).toMatch(/^update-user:/)
  })

  it('keeps the works-in-shifts marker of a wrongly typed name when the CNIC is set', async () => {
    const ali = user({
      user_id: '04050', uid: '57', display_name: 'Ali-S-A4230111028815', cnic_masked: null,
      cnic_available: false, identity_complete: false, machine_name_preview: 'Ali-S-A4230111028815',
      cnic_format_suspect: true, cnic_format_suspect_reason: 'LETTER_BEFORE_DIGITS',
      suggested_display_name: 'Ali', suggested_shift_worker: true, shift_worker: false,
    })
    const fetchMock = vi.fn(async () => response({ command, user: ali }))
    vi.stubGlobal('fetch', fetchMock)
    render(<DialogHarness state={{ mode: 'edit', user: ali }} />)
    expect((screen.getByLabelText(/Works in shifts/) as HTMLInputElement).checked).toBe(true)
    fireEvent.change(screen.getByLabelText(/CNIC \(needed — none saved yet\)/), { target: { value: '4230111028815' } })
    expect(screen.getByText('Ali-S-4230111028815')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'test-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const body = JSON.parse(String((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].body))
    expect(body.display_name).toBe('Ali')
    expect(body.shift_worker).toBe(true)
  })

  it('shows field errors on press instead of a grey button, and sends nothing', async () => {
    const fetchMock = vi.fn(async () => response({ command, user: user() }))
    vi.stubGlobal('fetch', fetchMock)
    render(<DialogHarness state={{ mode: 'create' }} />)
    fireEvent.change(screen.getByLabelText('CNIC'), { target: { value: '123456789' } })
    const add = screen.getByRole('button', { name: 'Add person' }) as HTMLButtonElement
    expect(add.disabled).toBe(false)
    fireEvent.click(add)
    expect(await screen.findByText('Type the full name.')).toBeTruthy()
    expect(screen.getByText('CNIC must be 13 digits (9 typed).')).toBeTruthy()
    expect(screen.getByText('Type your password.')).toBeTruthy()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('opens Edit for the exact person from an attendance link', async () => {
    const missingCnic = user({ id: 10, user_key: 'user-ten', user_id: '1010', display_name: 'Dr Farzana', cnic_masked: null, cnic_available: false, identity_complete: false, machine_name_preview: null })
    const fetchMock = workspaceFetch({ rows: [missingCnic] })
    vi.stubGlobal('fetch', fetchMock)
    window.history.replaceState(null, '', '/people/connector-one?user_id=1010')
    render(<UsersHarness />)
    expect(await screen.findByRole('heading', { name: 'Edit Dr Farzana' })).toBeTruthy()
    expect((screen.getByLabelText(/CNIC \(needed — none saved yet\)/) as HTMLInputElement).required).toBe(true)
    expect(fetchMock.mock.calls.some(([input]) => params(input).get('q') === '1010')).toBe(true)
  })

  it('says when the linked person is not on this machine and clears the search', async () => {
    vi.stubGlobal('fetch', workspaceFetch({ rows: [] }))
    window.history.replaceState(null, '', '/people/connector-one?user_id=1010')
    render(<UsersHarness />)
    expect((await screen.findByTestId('toast')).textContent).toBe("Employee ID 1010 isn't on this machine.")
    await waitFor(() => expect((screen.getByLabelText('Search name, Employee ID or CNIC') as HTMLInputElement).value).toBe(''))
  })

  it('opens the person sheet from ?q= when one person has that Employee ID', async () => {
    vi.stubGlobal('fetch', workspaceFetch())
    window.history.replaceState(null, '', '/people/connector-one?q=1007')
    render(<UsersHarness />)
    expect(await screen.findByRole('dialog', { name: 'Ayesha Khan' })).toBeTruthy()
  })

  it('shows one summary line, counted tabs, the row change and the reasons for each action', async () => {
    const limitedDevice: Device = { ...device, zkt: { ...device.zkt!, capabilities: { user_write: true } } }
    const busyUser = user({ id: 2, user_key: 'user-two', uid: '8', user_id: '1008', display_name: 'Bilal Ahmed', current_command_state: 'RUNNING' })
    vi.stubGlobal('fetch', workspaceFetch({ selectedDevice: limitedDevice, rows: [user(), busyUser] }))
    const view = render(<UsersHarness selectedDevice={limitedDevice} />)

    await screen.findByRole('article', { name: /Ayesha Khan, Employee ID 1007/i })
    const summary = document.querySelector('.people-summary-line')!
    expect(summary.textContent).toContain('1 need fixing')
    expect(summary.textContent).toContain('1 of 2 have a CNIC')
    expect(summary.textContent).not.toMatch(/%/)
    expect(screen.getByRole('tab', { name: /People/ })).toBeTruthy()
    expect(screen.getByRole('tab', { name: /Same CNIC/ })).toBeTruthy()
    expect(screen.getByText('Sending to machine…')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Add person' })).toBeNull()
    expect(screen.getByText("This machine doesn't allow adding people from the dashboard.")).toBeTruthy()
    expect((screen.getByRole('button', { name: /Edit Ayesha/i }) as HTMLButtonElement).disabled).toBe(false)
    expect((screen.getByRole('button', { name: /Edit Bilal/i }) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(screen.getByLabelText('More for Ayesha Khan'))
    const menu = screen.getByRole('group', { name: 'Options for Ayesha Khan' })
    expect(within(menu).queryByText('Temporary admin access')).toBeNull()
    expect((within(menu).getByRole('button', { name: 'Remove Ayesha Khan' }) as HTMLButtonElement).disabled).toBe(true)
    expect(within(menu).getByText("This machine doesn't allow removing people from the dashboard.")).toBeTruthy()
    expectPlainText(view.container, { skip: '[data-typed-phrase]' })
  })

  it('never shows 100% when one person in 300 has no CNIC', async () => {
    vi.stubGlobal('fetch', workspaceFetch({ counts: integrity({ total_users: 300, with_cnic: 299, missing_cnic: 1 }) }))
    render(<UsersHarness />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    expect(document.querySelector('.people-summary-line')?.textContent).toContain('299 of 300 have a CNIC')
    expect(document.body.textContent).not.toContain('100%')
  })

  it('keeps loaded people through Show more and searches a CNIC without putting it in the address', async () => {
    const older = user({ id: 2, user_key: 'user-two', uid: '8', user_id: '1008', display_name: 'Bilal Ahmed' })
    const fetchMock = workspaceFetch({ nextOffset: 200, pageRows: [older] })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })

    fireEvent.click(screen.getByRole('button', { name: 'Show more people' }))
    expect(await screen.findByRole('article', { name: /Bilal Ahmed/i })).toBeTruthy()
    expect(screen.getByRole('article', { name: /Ayesha Khan/i })).toBeTruthy()
    expect(fetchMock.mock.calls.some(([input]) => params(input).get('offset') === '200')).toBe(true)

    const search = screen.getByLabelText('Search name, Employee ID or CNIC')
    fireEvent.change(search, { target: { value: 'Ayesha' } })
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => params(input).get('q') === 'Ayesha')).toBe(true))
    fireEvent.change(search, { target: { value: '35202-1234567-1' } })
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => params(input).get('cnic') === '3520212345671')).toBe(true))
    expect(screen.getByRole('button', { name: /CNIC ending 5671/ })).toBeTruthy()
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 400)) })
    expect(window.location.search).not.toMatch(/3520212345671|cnic/i)
    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }))
    expect((search as HTMLInputElement).value).toBe('')
  })

  it('hints instead of failing while a CNIC is still being typed', async () => {
    vi.stubGlobal('fetch', workspaceFetch())
    render(<UsersHarness />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.change(screen.getByLabelText('Search name, Employee ID or CNIC'), { target: { value: '3520212345' } })
    expect(screen.getByText('Type all 13 digits to search by CNIC')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('maps the quick views and problem filters to the server filters', async () => {
    const fetchMock = workspaceFetch()
    vi.stubGlobal('fetch', fetchMock)
    window.history.replaceState(null, '', '/people/connector-one?problem=typed')
    render(<UsersHarness />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    expect(fetchMock.mock.calls.some(([input]) => params(input).get('identity') === 'TYPED_WRONG')).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: /Remove filter: CNIC typed wrongly/ }))
    fireEvent.click(screen.getByRole('button', { name: /^Needs fixing/ }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => params(input).get('identity') === 'MISSING')).toBe(true))
    expect(window.location.search).toContain('show=fixing')
    fireEvent.click(screen.getByRole('button', { name: /^Added this week/ }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => params(input).get('added_within_days') === '7')).toBe(true))
  })

  it('keeps ticks across searches and only ticks the current page', async () => {
    const ayesha = user()
    const bilal = user({ id: 2, user_key: 'user-two', uid: '8', user_id: '1008', display_name: 'Bilal Ahmed' })
    const baseFetch = workspaceFetch({ rows: [ayesha, bilal] })
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname === '/api/v2/devices/connector-one/users') {
        const visibleRows = url.searchParams.get('q') === 'Bilal' ? [bilal] : [ayesha, bilal]
        return Promise.resolve(response({ rows: visibleRows, next_cursor: null, next_offset: null, filtered_total: visibleRows.length, device, identity_integrity: integrity({ with_cnic: 2, missing_cnic: 0 }) }))
      }
      return baseFetch(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness />)

    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.click(screen.getByLabelText('Select Ayesha Khan'))
    expect(screen.getByText('1 selected')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Search name, Employee ID or CNIC'), { target: { value: 'Bilal' } })
    await waitFor(() => expect(screen.queryByRole('article', { name: /Ayesha Khan/i })).toBeNull())
    expect(screen.getByRole('article', { name: /Bilal Ahmed/i })).toBeTruthy()
    expect(screen.getByText('1 selected')).toBeTruthy()
    expect(screen.getByText(/0 of 1 on this page selected · 1 selected in total/i)).toBeTruthy()

    fireEvent.click(screen.getByLabelText('Select Bilal Ahmed'))
    expect(screen.getByText('2 selected')).toBeTruthy()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all on this page' }))
    expect(screen.getByText('1 selected')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Search name, Employee ID or CNIC'), { target: { value: '' } })
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    expect((screen.getByLabelText('Select Ayesha Khan') as HTMLInputElement).checked).toBe(true)
    expect((screen.getByLabelText('Select Bilal Ahmed') as HTMLInputElement).checked).toBe(false)
  })

  it('never lets a machine admin or a damaged entry be ticked or removed', async () => {
    const admin = user({ id: 3, user_key: 'user-admin', user_id: '1', display_name: 'Machine Owner', privilege: 14 })
    const damaged = user({ id: 4, user_key: 'user-bad', user_id: '1234567890123', display_name: 'tt', looks_damaged: true })
    vi.stubGlobal('fetch', workspaceFetch({ rows: [admin, damaged] }))
    render(<UsersHarness />)
    await screen.findByRole('article', { name: /Machine Owner/ })
    expect((screen.getByLabelText('Select Machine Owner') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByLabelText('Select tt') as HTMLInputElement).disabled).toBe(true)
    expect(screen.getByText('Damaged entry')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('More for tt'))
    const menu = screen.getByRole('group', { name: 'Options for tt' })
    expect((within(menu).getByRole('button', { name: 'Remove tt' }) as HTMLButtonElement).disabled).toBe(true)
    expect(within(menu).getByText(/Removing it from the dashboard can erase every fingerprint/)).toBeTruthy()
  })

  it('still needs the typed Employee ID or name and the password to remove one person', async () => {
    const fetchMock = vi.fn(async () => response({ command, user: user() }))
    vi.stubGlobal('fetch', fetchMock)
    render(<DialogHarness state={{ mode: 'delete', user: user() }} />)
    const remove = screen.getByRole('button', { name: 'Remove from machine' }) as HTMLButtonElement
    expect(remove.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText(/To confirm, type their Employee ID \(1007\) or full name/), { target: { value: '1007' } })
    expect(remove.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'pw' } })
    expect(remove.disabled).toBe(false)
    fireEvent.change(screen.getByLabelText(/To confirm, type their Employee ID/), { target: { value: '1008' } })
    expect(remove.disabled).toBe(true)
    fireEvent.click(remove)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('re-checks the exact selection before opening and again before sending', async () => {
    const ayesha = user()
    const bilal = user({ id: 2, user_key: 'user-two', uid: '8', user_id: '1008', display_name: 'Bilal Ahmed' })
    const baseFetch = workspaceFetch({ rows: [ayesha, bilal] })
    let validationCalls = 0
    let creationCalls = 0
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname.endsWith('/users/validate-selection')) {
        validationCalls += 1
        return Promise.resolve(response({ rows: validationCalls === 1 ? [ayesha, bilal] : [bilal], missing_user_keys: validationCalls === 1 ? [] : [ayesha.user_key] }))
      }
      if (url.pathname.endsWith('/user-deletion-jobs') && init?.method === 'POST') creationCalls += 1
      return baseFetch(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness />)

    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.click(screen.getByLabelText('Select Ayesha Khan'))
    fireEvent.click(screen.getByLabelText('Select Bilal Ahmed'))
    fireEvent.click(screen.getByRole('button', { name: 'Remove 2' }))

    expect(await screen.findByRole('heading', { name: /Remove 2 people from/i })).toBeTruthy()
    expect(validationCalls).toBe(1)
    fireEvent.change(screen.getByLabelText(/Why\? \(saved in the record\)/), { target: { value: 'Remove people who left' } })
    fireEvent.change(screen.getByLabelText(/To confirm, type exactly/), { target: { value: 'DELETE 2 USERS FROM 1' } })
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'correct-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Remove 2 people' }))

    expect(await screen.findByText(/The list changed while this was open/i)).toBeTruthy()
    expect(screen.getByRole('heading', { name: /Remove 1 person from/i })).toBeTruthy()
    expect(within(screen.getByRole('dialog')).queryByText(/Ayesha Khan/)).toBeNull()
    expect(within(screen.getByRole('dialog')).getByText(/Bilal Ahmed/)).toBeTruthy()
    expect(validationCalls).toBe(2)
    expect(creationCalls).toBe(0)
  })

  it('needs the reason, the exact server phrase and the password to remove several people', async () => {
    const ayesha = user()
    const baseFetch = workspaceFetch({ rows: [ayesha] })
    let creationCalls = 0
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname.endsWith('/users/validate-selection')) return Promise.resolve(response({ rows: [ayesha], missing_user_keys: [] }))
      if (url.pathname.endsWith('/user-deletion-jobs') && init?.method === 'POST') creationCalls += 1
      return baseFetch(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.click(screen.getByLabelText('Select Ayesha Khan'))
    fireEvent.click(screen.getByRole('button', { name: 'Remove 1' }))
    await screen.findByRole('heading', { name: /Remove 1 person from/i })
    const submit = screen.getByRole('button', { name: 'Remove 1 person' })
    fireEvent.change(screen.getByLabelText(/Why\?/), { target: { value: 'short' } })
    fireEvent.click(submit)
    expect(await screen.findByText('Write a short reason (at least 10 letters).')).toBeTruthy()
    fireEvent.change(screen.getByLabelText(/Why\?/), { target: { value: 'Left the company in October' } })
    fireEvent.change(screen.getByLabelText(/To confirm, type exactly/), { target: { value: 'REMOVE 1 PERSON' } })
    fireEvent.click(submit)
    expect(await screen.findByText("The confirmation doesn't match. Type it exactly as shown.")).toBeTruthy()
    fireEvent.change(screen.getByLabelText(/To confirm, type exactly/), { target: { value: 'DELETE 1 USERS FROM 1' } })
    fireEvent.click(submit)
    expect(await screen.findByText('Type your password.')).toBeTruthy()
    expect(creationCalls).toBe(0)
  })

  it('sends the refreshed row version after the selection check', async () => {
    const ayesha = user()
    const refreshed = user({ row_version: 4, display_name: 'Ayesha Khan Updated' })
    const baseFetch = workspaceFetch({ rows: [ayesha] })
    const creationBodies: Array<{ targets?: Array<{ user_key: string; expected_version: number }>; typed_confirmation?: string }> = []
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname.endsWith('/users/validate-selection')) return Promise.resolve(response({ rows: [refreshed], missing_user_keys: [] }))
      if (url.pathname.endsWith('/user-deletion-jobs') && init?.method === 'POST') {
        creationBodies.push(JSON.parse(String(init.body)))
        return Promise.resolve(response({
          job: {
            job_id: 'job-one', connector_id: device.connector_id, status: 'SUCCEEDED', reason: 'Remove obsolete identity',
            counts: { requested: 1, succeeded: 1, failed: 0, canceled: 0, expired: 0, pending: 0 },
            created_at: '2026-08-12T10:00:00Z', expires_at: '2026-08-13T10:00:00Z',
            started_at: '2026-08-12T10:00:01Z', completed_at: new Date().toISOString(),
            items: [{ user_key: 'user-one', uid: '7', user_id: '1007', display_name: 'Ayesha Khan Updated', status: 'SUCCEEDED', error_code: null, error_message: null, result: {} }],
          },
        }, 202))
      }
      return baseFetch(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness />)

    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.click(screen.getByLabelText('Select Ayesha Khan'))
    fireEvent.click(screen.getByRole('button', { name: 'Remove 1' }))
    expect(await screen.findByText(/Ayesha Khan Updated/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText(/Why\?/), { target: { value: 'Remove obsolete identity' } })
    fireEvent.change(screen.getByLabelText(/To confirm, type exactly/), { target: { value: 'DELETE 1 USERS FROM 1' } })
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'correct-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Remove 1 person' }))

    await waitFor(() => expect(creationBodies).toHaveLength(1))
    expect(creationBodies[0]?.targets).toEqual([{ user_key: ayesha.user_key, expected_version: 4 }])
    expect(creationBodies[0]?.typed_confirmation).toBe('DELETE 1 USERS FROM 1')
    await waitFor(() => expect(document.querySelector('.people-removal-items')?.textContent).toContain('Ayesha Khan Updated — removed'))
  })

  it('uses one virtualized list from the first full page through Show more', async () => {
    const firstPage = Array.from({ length: 200 }, (_, index) => user({ id: index + 1, user_key: `user-${index + 1}`, uid: `${index + 1}`, user_id: `${10_000 + index}`, display_name: `Operator ${index + 1}` }))
    const finalUser = user({ id: 201, user_key: 'user-201', uid: '201', user_id: '10200', display_name: 'Operator 201' })
    vi.stubGlobal('fetch', workspaceFetch({ rows: firstPage, nextOffset: 200, pageRows: [finalUser] }))
    render(<UsersHarness />)

    await waitFor(() => expect(document.querySelector<HTMLElement>('.user-directory-table')?.classList.contains('is-virtualized')).toBe(true))
    const directory = document.querySelector<HTMLElement>('.user-directory-table')
    fireEvent.click(screen.getByRole('button', { name: 'Show more people' }))
    await waitFor(() => expect(document.querySelector('.people-result-line')?.textContent).toContain('Showing 201 of'))
    expect(document.querySelector<HTMLElement>('.user-directory-table')).toBe(directory)
    expect(directory?.classList.contains('is-virtualized')).toBe(true)
  })

  it('reloads the rows already shown on a live update, not just the first page', async () => {
    const firstPage = Array.from({ length: 200 }, (_, index) => user({ id: index + 1, user_key: `user-${index + 1}`, uid: `${index + 1}`, user_id: `${10_000 + index}`, display_name: `Operator ${index + 1}` }))
    const more = Array.from({ length: 200 }, (_, index) => user({ id: index + 201, user_key: `user-${index + 201}`, uid: `${index + 201}`, user_id: `${20_000 + index}`, display_name: `Operator ${index + 201}` }))
    const fetchMock = workspaceFetch({ rows: firstPage, nextOffset: 200, pageRows: more })
    vi.stubGlobal('fetch', fetchMock)
    const view = render(<UsersHarness />)
    await waitFor(() => expect(document.querySelector('.people-result-line')?.textContent).toContain('Showing 200'))
    fireEvent.click(screen.getByRole('button', { name: 'Show more people' }))
    await waitFor(() => expect(document.querySelector('.people-result-line')?.textContent).toContain('Showing 400'))
    view.rerender(<UsersHarness revision={1} />)
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => params(input).get('limit') === '400')).toBe(true))
    await waitFor(() => expect(document.querySelector('.people-result-line')?.textContent).toContain('Showing 400'))
  })

  it('clears the search, ticks, dialogs and tab when the device changes, and keeps the quick view', async () => {
    const secondDevice: Device = { ...device, connector_id: 'connector-two', hardware_id: 'hw-two', device_id: '2', display_name: 'Islamabad Zone Office', friendly_name: 'Islamabad', zkt: { ...device.zkt!, id: 2, serial: 'SERIAL-TWO', expected_serial: 'SERIAL-TWO' } }
    vi.stubGlobal('fetch', workspaceFetch())
    const view = render(<UsersHarness />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.change(screen.getByLabelText('Search name, Employee ID or CNIC'), { target: { value: 'Ayesha' } })
    fireEvent.click(screen.getByRole('button', { name: /^Needs fixing/ }))
    fireEvent.click(screen.getByLabelText('Select Ayesha Khan'))
    expect(screen.getByText('1 selected')).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: /Same CNIC/ }))
    fireEvent.click(screen.getByRole('tab', { name: /People/ }))
    fireEvent.click(await screen.findByRole('button', { name: /Edit Ayesha/i }))
    expect(screen.getByRole('heading', { name: /Edit Ayesha Khan/i })).toBeTruthy()

    view.rerender(<UsersHarness selectedDevice={secondDevice} />)
    await waitFor(() => expect((screen.getByLabelText('Search name, Employee ID or CNIC') as HTMLInputElement).value).toBe(''))
    expect(screen.getByRole('tab', { name: /People/ }).getAttribute('aria-selected')).toBe('true')
    expect(screen.queryByRole('heading', { name: /Edit Ayesha Khan/i })).toBeNull()
    expect(screen.queryByText('1 selected')).toBeNull()
    expect(screen.getByRole('button', { name: /^Needs fixing/ }).getAttribute('aria-pressed')).toBe('true')
  })

  it('keeps the list when a part fails, shows temporary admin access and refreshes the list from the machine', async () => {
    const expires = new Date(Date.now() + 7 * 60_000 + 42_000).toISOString()
    const leasedDevice = {
      ...device,
      active_lease: {
        lease_id: 'lease-one', state: 'ACTIVE', requested_at: '2026-08-12T09:55:00Z', granted_at: '2026-08-12T09:56:00Z',
        expires_at: expires, revoked_at: null, last_error: null, user_id: '1007', user_display_name: 'Ayesha Khan',
      },
    } as Device
    const fetchMock = workspaceFetch({ selectedDevice: leasedDevice, failIdentity: true })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness selectedDevice={leasedDevice} />)
    expect(await screen.findByRole('article', { name: /Ayesha Khan/i })).toBeTruthy()
    const banner = screen.getByRole('region', { name: 'Temporary admin access' })
    expect(banner.textContent).toContain('Ayesha Khan (Employee ID 1007)')
    expect(banner.textContent).toMatch(/Ends in 0[67]:\d\d/)

    fireEvent.click(screen.getByRole('tab', { name: /Same CNIC/ }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/Couldn't check for same CNICs/)
    fireEvent.click(within(banner).getByRole('button', { name: 'End now' }))
    expect(screen.getByRole('heading', { name: 'End temporary admin access now?' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Keep it' }))
    fireEvent.click(screen.getByRole('tab', { name: /People/ }))

    fireEvent.click(screen.getByRole('button', { name: 'Refresh people list from the machine' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => String(input).includes('/users/refresh') && init?.method === 'POST')).toBe(true))
    expect(await screen.findByText(/Asked the machine for its list of people/i)).toBeTruthy()
    expect(await screen.findByRole('region', { name: 'Changes on this machine' })).toBeTruthy()
    expect(screen.getByText('Refreshing the people list from the machine')).toBeTruthy()
  })

  it('lets an admin confirm the machine from the People page', async () => {
    const awaitingDevice: Device = {
      ...device, state: 'OFFLINE', connected: false,
      zkt: {
        ...device.zkt!, serial: 'ANM4261301077', expected_serial: null, confirmed_serial: null,
        terminal_binding_state: 'SERIAL_CONFIRMATION_REQUIRED', certification_state: 'READ_ONLY',
        writes_disabled_reason: 'TERMINAL_SERIAL_CONFIRMATION_REQUIRED', online: false, connection_state: 'OFFLINE',
        capabilities: { create_user: false, user_write: false, admin_lease: false, delete_user: false },
      },
    }
    const pendingDevice: Device = { ...awaitingDevice, zkt: { ...awaitingDevice.zkt!, expected_serial: 'ANM4261301077', confirmed_serial: 'ANM4261301077', terminal_binding_state: 'PENDING_DEVICE_ACK', writes_disabled_reason: 'TERMINAL_SERIAL_PENDING_DEVICE_ACK' } }
    const baseFetch = workspaceFetch({ selectedDevice: awaitingDevice })
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/terminal-binding/confirm') && init?.method === 'POST') {
        return Promise.resolve(response({ device: pendingDevice, command: { ...command, type: 'PIN_TERMINAL_SERIAL', status: 'WAITING_FOR_DEVICE' } }, 202))
      }
      return baseFetch(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness selectedDevice={awaitingDevice} />)

    expect(await screen.findByRole('heading', { name: 'Is this the right machine?' })).toBeTruthy()
    expect(screen.getAllByText(/ANM4261301077/).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: /Yes, this is the right machine/i }))
    expect(screen.getByRole('heading', { name: 'Confirm this machine' })).toBeTruthy()
    expect(screen.getByText(/Your confirmation waits up to 10 minutes/i)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'correct-password' } })
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm machine' }))

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => {
      if (!String(input).includes('/terminal-binding/confirm') || init?.method !== 'POST') return false
      const body = JSON.parse(String(init.body))
      return body.observed_serial === 'ANM4261301077' && body.password === 'correct-password' && String(body.idempotency_key).startsWith('terminal-serial-confirmation:')
    })).toBe(true))
    expect(await screen.findByRole('heading', { name: 'Waiting for the link box to save it' })).toBeTruthy()
    expect(screen.getByText(/It will be sent when the link box is back online/i)).toBeTruthy()
  })

  it('offers to confirm a moved-over machine again', async () => {
    const legacyDevice: Device = { ...device, zkt: { ...device.zkt!, expected_serial: null, confirmed_serial: device.zkt!.serial, terminal_binding_state: 'CONFIRMED', serial_confirmed_by: 'MIGRATED_PREEXISTING' } }
    vi.stubGlobal('fetch', workspaceFetch({ selectedDevice: legacyDevice }))
    render(<UsersHarness selectedDevice={legacyDevice} />)
    expect(await screen.findByRole('heading', { name: 'Is this the right machine?' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Yes, this is the right machine/i }))
    expect(screen.getByRole('heading', { name: 'Confirm this machine again' })).toBeTruthy()
  })

  it('ignores an old search answer and says when nothing changed instead of sending', async () => {
    let resolveSlow: ((value: Response) => void) | null = null
    const fetchMock = workspaceFetch()
    const routedFetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname.includes('/api/v2/devices/connector-one/users') && url.searchParams.get('q') === 'slow') return new Promise<Response>((resolve) => { resolveSlow = resolve })
      if (url.pathname.includes('/api/v2/devices/connector-one/users') && url.searchParams.get('q') === 'fast') {
        return Promise.resolve(response({ rows: [user({ display_name: 'Fast Result' })], next_cursor: null, next_offset: null, device, identity_integrity: integrity({ total_users: 1, with_cnic: 1, missing_cnic: 0 }) }))
      }
      if (url.pathname.endsWith('/users/user-one') && init?.method === 'PATCH') return Promise.resolve(response({ command, user: user() }))
      return fetchMock(input, init)
    })
    vi.stubGlobal('fetch', routedFetch)
    render(<UsersHarness />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    const search = screen.getByLabelText('Search name, Employee ID or CNIC')
    fireEvent.change(search, { target: { value: 'slow' } })
    await waitFor(() => expect(resolveSlow).toBeTruthy())
    fireEvent.change(search, { target: { value: 'fast' } })
    expect(await screen.findByRole('article', { name: /Fast Result/i })).toBeTruthy()
    await act(async () => resolveSlow?.(response({ rows: [user({ display_name: 'Stale Result' })], next_cursor: null, device, identity_integrity: integrity() })))
    expect(screen.queryByRole('article', { name: /Stale Result/i })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Edit Fast Result/i }))
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'password' } })
    const save = screen.getByRole('button', { name: 'Save changes' }) as HTMLButtonElement
    expect(save.disabled).toBe(false)
    fireEvent.click(save)
    expect(await screen.findByText('Nothing changed yet.')).toBeTruthy()
    expect(routedFetch.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false)
    fireEvent.change(screen.getByLabelText('Full name'), { target: { value: 'Fast Result Updated' } })
    fireEvent.click(save)
    await waitFor(() => expect(routedFetch.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(true))
  })

  it('adds no reason, password or typed field to any one-click action on this page', async () => {
    vi.stubGlobal('fetch', workspaceFetch())
    render(<UsersHarness />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    expect(document.querySelector('input[type="password"]')).toBeNull()
    expect(screen.queryByRole('button', { name: /reconcile|release|update/i })).toBeNull()
  })
})
