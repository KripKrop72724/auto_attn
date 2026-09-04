import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useToast } from '../App'
import type { Device, DeviceUser } from '../types'
import { UsersView } from './UsersWorkspace'

const device: Device = {
  connector_id: 'connector-one', hardware_id: 'hw-one', zone_id: 'ZONE-ONE', zone_name: 'Islamabad',
  device_id: '1', display_name: 'SLICTOWER · 3rd Floor', state: 'ONLINE', connected: true,
  firmware_version: '2.4.5', onboarding_generation: 2, last_onboarded_at: null,
  last_seen_at: '2026-08-12T10:00:00Z', current_activity: 'Live capture', last_error_code: null,
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

const user = (overrides: Partial<DeviceUser> = {}): DeviceUser => ({
  id: 1, user_key: 'user-one', uid: '7', user_id: '1007', display_name: 'Ayesha Khan',
  cnic_masked: '*****-****567-1', cnic_available: true, identity_complete: true,
  identity_conflict_code: null, identity_conflict_members: [], identity_conflict_resolved: false,
  identity_resolution_id: null, shift_worker: false, privilege: 0, present: true, lifecycle_state: 'ACTIVE',
  row_version: 3, observed_at: '2026-08-12T08:00:00Z', machine_name_preview: 'Ayesha-*****-****567-1',
  current_command_state: null, read_only: false, ...overrides,
})

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

const command = {
  command_id: 'command-one', type: 'REFRESH_USERS', status: 'QUEUED', created_at: '2026-08-12T10:00:00Z', expires_at: null,
}

function workspaceFetch({
  rows = [user()],
  selectedDevice = device,
  nextCursor = null as number | null,
  failIdentity = false,
  pageRows = [] as DeviceUser[],
} = {}) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path.includes('/users/refresh') && init?.method === 'POST') return response(command)
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
    if (path.includes('/api/v2/devices/connector-one/users')) {
      const isPage = new URL(path, 'https://add.test').searchParams.has('cursor')
      return response({
        rows: isPage ? pageRows : rows,
        next_cursor: isPage ? null : nextCursor,
        device: selectedDevice,
        identity_integrity: {
          source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT', total_users: 2, with_cnic: 1, missing_cnic: 1,
          duplicate_groups: 0, duplicate_users: 0, resolved_duplicate_groups: 0,
          unresolved_duplicate_groups: 0, unresolved_duplicate_users: 0,
        },
      })
    }
    return response({ rows: [] })
  })
}

function UsersHarness({ selectedDevice = device, rows = [user()], revision = 0 }: { selectedDevice?: Device; rows?: DeviceUser[]; revision?: number }) {
  const toast = useToast()
  return <><UsersView devices={[selectedDevice]} selectedDeviceId={selectedDevice.connector_id} onSelectDevice={() => {}} revision={revision} toast={toast} refreshFleet={async () => {}} />{toast.toast && <div role={toast.toast.kind === 'error' ? 'alert' : 'status'}>{toast.toast.text}</div>}</>
}

describe('Selected-terminal users workspace', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    window.history.replaceState(null, '', '/')
  })

  it('opens the exact missing-CNIC employee from an attendance review deep link', async () => {
    const missingCnic = user({
      id: 10,
      user_key: 'user-ten',
      user_id: '1010',
      display_name: 'Dr Farzana',
      cnic_masked: null,
      cnic_available: false,
      identity_complete: false,
      machine_name_preview: null,
    })
    const fetchMock = workspaceFetch({ rows: [missingCnic] })
    vi.stubGlobal('fetch', fetchMock)
    window.history.replaceState(null, '', '/users/connector-one?user_id=1010')

    render(<UsersHarness rows={[missingCnic]} />)

    expect(await screen.findByRole('heading', { name: 'Edit device user' })).toBeTruthy()
    const cnic = screen.getByLabelText(
      'Replacement CNIC (required for missing CNIC)',
    ) as HTMLInputElement
    expect(cnic.required).toBe(true)
    expect(
      fetchMock.mock.calls.some(([input]) =>
        new URL(String(input), 'https://add.test').searchParams.get('q') === '1010'),
    ).toBe(true)
  })

  it('shows trusted metrics, counted tabs, per-row command state, and action-specific capability reasons', async () => {
    const limitedDevice: Device = {
      ...device,
      zkt: { ...device.zkt!, capabilities: { user_write: true } },
    }
    const busyUser = user({ id: 2, user_key: 'user-two', uid: '8', user_id: '1008', display_name: 'Bilal Ahmed', current_command_state: 'RUNNING' })
    vi.stubGlobal('fetch', workspaceFetch({ selectedDevice: limitedDevice, rows: [user(), busyUser] }))
    render(<UsersHarness selectedDevice={limitedDevice} rows={[user(), busyUser]} />)

    await screen.findByRole('article', { name: /Ayesha Khan, user 1007/i })
    const metrics = screen.getByLabelText('Selected terminal user indicators')
    expect(within(metrics).getByText('CNIC complete').closest('article')?.textContent).toContain('50%')
    expect(within(metrics).getByText('Identity attention').closest('article')?.textContent).toContain('1')
    expect(screen.getByRole('tab', { name: /Directory2/i })).toBeTruthy()
    expect(screen.getByRole('tab', { name: /Identity Review0/i })).toBeTruthy()
    expect(screen.getByText('RUNNING')).toBeTruthy()

    const add = screen.getByRole('button', { name: 'Add user' }) as HTMLButtonElement
    expect(add.disabled).toBe(true)
    expect(screen.getByText(/does not advertise the certified create-user capability/i)).toBeTruthy()
    expect((screen.getByRole('button', { name: /Edit Ayesha/i }) as HTMLButtonElement).disabled).toBe(false)
    expect((screen.getByRole('button', { name: /Edit Bilal/i }) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(screen.getByLabelText(/More actions for Ayesha/i))
    const menu = screen.getByRole('group', { name: /Actions for Ayesha/i })
    expect(menu.closest('.panel')).toBeNull()
    expect((within(menu).getByRole('button', { name: /Enrollment access/i }) as HTMLButtonElement).disabled).toBe(true)
    expect((within(menu).getByRole('button', { name: /Delete Ayesha/i }) as HTMLButtonElement).disabled).toBe(true)
    expect(within(menu).getByText(/certified temporary enrollment access/i)).toBeTruthy()
    expect(within(menu).getByText(/certified delete-user capability/i)).toBeTruthy()
  })

  it('keeps loaded rows through pagination and separates general search from exact CNIC', async () => {
    const older = user({ id: 2, user_key: 'user-two', uid: '8', user_id: '1008', display_name: 'Bilal Ahmed' })
    const fetchMock = workspaceFetch({ nextCursor: 10, pageRows: [older] })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })

    fireEvent.click(screen.getByRole('button', { name: /Load more terminal users/i }))
    expect(screen.getByRole('article', { name: /Ayesha Khan/i })).toBeTruthy()
    expect(await screen.findByRole('article', { name: /Bilal Ahmed/i })).toBeTruthy()

    fireEvent.change(screen.getByLabelText(/Search user name, user ID, or UID/i), { target: { value: 'Ayesha' } })
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => new URL(String(input), 'https://add.test').searchParams.get('q') === 'Ayesha')).toBe(true))
    fireEvent.change(screen.getByLabelText(/Exact CNIC search/i), { target: { value: '3520212345671' } })
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => new URL(String(input), 'https://add.test').searchParams.get('cnic') === '3520212345671')).toBe(true))
    expect(screen.getByLabelText('Active user filters').textContent).toMatch(/Search: Ayesha/i)
    expect(screen.getByLabelText('Active user filters').textContent).toMatch(/Exact CNIC: 3520212345671/i)
    fireEvent.click(within(screen.getByLabelText('Active user filters')).getByRole('button', { name: 'Clear all' }))
    expect((screen.getByLabelText(/Exact CNIC search/i) as HTMLInputElement).value).toBe('')
  })

  it('preserves terminal-scoped multi-selection across searches and only toggles the current view', async () => {
    const ayesha = user()
    const bilal = user({ id: 2, user_key: 'user-two', uid: '8', user_id: '1008', display_name: 'Bilal Ahmed' })
    const baseFetch = workspaceFetch({ rows: [ayesha, bilal] })
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname === '/api/v2/devices/connector-one/users') {
        const visibleRows = url.searchParams.get('q') === 'Bilal' ? [bilal] : [ayesha, bilal]
        return Promise.resolve(response({
          rows: visibleRows,
          next_cursor: null,
          device,
          identity_integrity: {
            source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT', total_users: 2, with_cnic: 2, missing_cnic: 0,
            duplicate_groups: 0, duplicate_users: 0, resolved_duplicate_groups: 0,
            unresolved_duplicate_groups: 0, unresolved_duplicate_users: 0,
          },
        }))
      }
      return baseFetch(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness />)

    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.click(screen.getByLabelText(/Select Ayesha Khan for bulk deletion/i))
    expect(screen.getByText('1 user selected')).toBeTruthy()

    fireEvent.change(screen.getByLabelText(/Search user name, user ID, or UID/i), { target: { value: 'Bilal' } })
    await waitFor(() => expect(screen.queryByRole('article', { name: /Ayesha Khan/i })).toBeNull())
    expect(screen.getByRole('article', { name: /Bilal Ahmed/i })).toBeTruthy()
    expect(screen.getByText('1 user selected')).toBeTruthy()
    expect(screen.getByText(/0 selected of 1 eligible in view · 1 total/i)).toBeTruthy()

    fireEvent.click(screen.getByLabelText(/Select Bilal Ahmed for bulk deletion/i))
    expect(screen.getByText('2 users selected')).toBeTruthy()
    fireEvent.click(screen.getByRole('checkbox', { name: /Select eligible users in this view/i }))
    expect(screen.getByText('1 user selected')).toBeTruthy()

    fireEvent.change(screen.getByLabelText(/Search user name, user ID, or UID/i), { target: { value: '' } })
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    expect((screen.getByLabelText(/Select Ayesha Khan for bulk deletion/i) as HTMLInputElement).checked).toBe(true)
    expect((screen.getByLabelText(/Select Bilal Ahmed for bulk deletion/i) as HTMLInputElement).checked).toBe(false)
  })

  it('revalidates the exact selection before opening and again before submitting', async () => {
    const ayesha = user()
    const bilal = user({ id: 2, user_key: 'user-two', uid: '8', user_id: '1008', display_name: 'Bilal Ahmed' })
    const baseFetch = workspaceFetch({ rows: [ayesha, bilal] })
    let validationCalls = 0
    let creationCalls = 0
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname.endsWith('/users/validate-selection')) {
        validationCalls += 1
        return Promise.resolve(response({
          rows: validationCalls === 1 ? [ayesha, bilal] : [bilal],
          missing_user_keys: validationCalls === 1 ? [] : [ayesha.user_key],
        }))
      }
      if (url.pathname.endsWith('/user-deletion-jobs') && init?.method === 'POST') {
        creationCalls += 1
      }
      return baseFetch(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness rows={[ayesha, bilal]} />)

    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.click(screen.getByLabelText(/Select Ayesha Khan for bulk deletion/i))
    fireEvent.click(screen.getByLabelText(/Select Bilal Ahmed for bulk deletion/i))
    fireEvent.click(screen.getByRole('button', { name: /Delete selected/i }))

    expect(await screen.findByRole('heading', { name: /Delete 2 terminal users/i })).toBeTruthy()
    expect(validationCalls).toBe(1)
    fireEvent.change(screen.getByLabelText(/Audit reason/i), { target: { value: 'Remove obsolete terminal identities' } })
    fireEvent.change(screen.getByLabelText(/Type “DELETE 2 USERS FROM 1”/i), { target: { value: 'DELETE 2 USERS FROM 1' } })
    fireEvent.change(screen.getByLabelText(/Confirm administrator password/i), { target: { value: 'correct-password' } })
    fireEvent.click(screen.getByRole('button', { name: /Delete 2 users safely/i }))

    expect(await screen.findByText(/selected users changed while this confirmation was open/i)).toBeTruthy()
    expect(screen.getByRole('heading', { name: /Delete 1 terminal users/i })).toBeTruthy()
    expect(within(screen.getByRole('dialog')).queryByText(/Ayesha Khan/)).toBeNull()
    expect(within(screen.getByRole('dialog')).getByText(/Bilal Ahmed/)).toBeTruthy()
    expect(validationCalls).toBe(2)
    expect(creationCalls).toBe(0)
  })

  it('submits the refreshed row version after selection validation', async () => {
    const ayesha = user()
    const refreshed = user({ row_version: 4, display_name: 'Ayesha Khan Updated' })
    const baseFetch = workspaceFetch({ rows: [ayesha] })
    const creationBodies: Array<{ targets?: Array<{ user_key: string; expected_version: number }> }> = []
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname.endsWith('/users/validate-selection')) {
        return Promise.resolve(response({ rows: [refreshed], missing_user_keys: [] }))
      }
      if (url.pathname.endsWith('/user-deletion-jobs') && init?.method === 'POST') {
        creationBodies.push(JSON.parse(String(init.body)))
        return Promise.resolve(response({
          job: {
            job_id: 'job-one', connector_id: device.connector_id, status: 'SUCCEEDED',
            reason: 'Remove obsolete terminal identity',
            counts: { requested: 1, succeeded: 1, failed: 0, canceled: 0, expired: 0, pending: 0 },
            created_at: '2026-08-12T10:00:00Z', expires_at: '2026-08-13T10:00:00Z',
            started_at: '2026-08-12T10:00:01Z', completed_at: '2026-08-12T10:00:02Z', items: [],
          },
        }, 202))
      }
      return baseFetch(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness rows={[ayesha]} />)

    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.click(screen.getByLabelText(/Select Ayesha Khan for bulk deletion/i))
    fireEvent.click(screen.getByRole('button', { name: /Delete selected/i }))
    expect(await screen.findByText(/Ayesha Khan Updated/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText(/Audit reason/i), { target: { value: 'Remove obsolete terminal identity' } })
    fireEvent.change(screen.getByLabelText(/Type “DELETE 1 USERS FROM 1”/i), { target: { value: 'DELETE 1 USERS FROM 1' } })
    fireEvent.change(screen.getByLabelText(/Confirm administrator password/i), { target: { value: 'correct-password' } })
    fireEvent.click(screen.getByRole('button', { name: /Delete 1 users safely/i }))

    await waitFor(() => expect(creationBodies).toHaveLength(1))
    expect(creationBodies[0]?.targets).toEqual([{ user_key: ayesha.user_key, expected_version: 4 }])
  })

  it('uses one bounded virtualized directory from the first full page through pagination', async () => {
    const firstPage = Array.from({ length: 200 }, (_, index) => user({
      id: index + 1,
      user_key: `user-${index + 1}`,
      uid: `${index + 1}`,
      user_id: `${10_000 + index}`,
      display_name: `Operator ${index + 1}`,
    }))
    const finalUser = user({ id: 201, user_key: 'user-201', uid: '201', user_id: '10200', display_name: 'Operator 201' })
    vi.stubGlobal('fetch', workspaceFetch({ rows: firstPage, nextCursor: 200, pageRows: [finalUser] }))
    render(<UsersHarness rows={firstPage} />)

    await waitFor(() => expect(document.querySelector<HTMLElement>('.user-directory-table')?.classList.contains('is-virtualized')).toBe(true))
    const directory = document.querySelector<HTMLElement>('.user-directory-table')

    fireEvent.click(screen.getByRole('button', { name: /Load more terminal users/i }))
    await waitFor(() => expect(document.querySelector('.user-directory-panel > .panel-header p')?.textContent).toContain('201 loaded'))
    expect(document.querySelector<HTMLElement>('.user-directory-table')).toBe(directory)
    expect(directory?.classList.contains('is-virtualized')).toBe(true)
  })

  it('resets filters, dialogs, selections, and the active section when the terminal changes', async () => {
    const secondDevice: Device = {
      ...device,
      connector_id: 'connector-two',
      hardware_id: 'hw-two',
      device_id: '2',
      display_name: 'Islamabad Zone Office',
      zkt: { ...device.zkt!, id: 2, serial: 'SERIAL-TWO', expected_serial: 'SERIAL-TWO' },
    }
    vi.stubGlobal('fetch', workspaceFetch())
    const view = render(<UsersHarness />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    fireEvent.change(screen.getByLabelText(/Search user name/i), { target: { value: 'Ayesha' } })
    fireEvent.change(screen.getByLabelText(/Identity completeness/i), { target: { value: 'MISSING' } })
    fireEvent.click(screen.getByLabelText(/Select Ayesha Khan for bulk deletion/i))
    expect(screen.getByText('1 user selected')).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: /Identity Review/i }))
    fireEvent.click(screen.getByRole('tab', { name: /Directory/i }))
    fireEvent.click(screen.getByRole('button', { name: /Edit Ayesha/i }))
    expect(screen.getByRole('heading', { name: /Edit device user/i })).toBeTruthy()

    view.rerender(<UsersHarness selectedDevice={secondDevice} />)
    expect((screen.getByLabelText(/Search user name/i) as HTMLInputElement).value).toBe('')
    expect((screen.getByLabelText(/Identity completeness/i) as HTMLSelectElement).value).toBe('ALL')
    expect(screen.getByRole('tab', { name: /Directory/i }).getAttribute('aria-selected')).toBe('true')
    expect(screen.queryByRole('heading', { name: /Edit device user/i })).toBeNull()
    expect(screen.queryByText('1 user selected')).toBeNull()
  })

  it('retains the directory during diagnostic failure, exposes active lease revocation, and distinguishes terminal sync', async () => {
    const leasedDevice: Device = {
      ...device,
      active_lease: {
        lease_id: 'lease-one', state: 'ACTIVE', requested_at: '2026-08-12T09:55:00Z', granted_at: '2026-08-12T09:56:00Z',
        expires_at: '2026-08-12T10:06:00Z', revoked_at: null, last_error: null,
      },
    }
    const fetchMock = workspaceFetch({ selectedDevice: leasedDevice, failIdentity: true })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness selectedDevice={leasedDevice} />)
    expect(await screen.findByRole('article', { name: /Ayesha Khan/i })).toBeTruthy()
    expect(screen.getByText(/temporary enrollment access/i)).toBeTruthy()

    fireEvent.click(screen.getByRole('tab', { name: /Identity Review/i }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/identity evidence is temporarily unavailable/i)
    fireEvent.click(screen.getByRole('button', { name: /Revoke access/i }))
    expect(screen.getByRole('heading', { name: /Revoke enrollment access/i })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('tab', { name: /Directory/i }))

    fireEvent.click(screen.getByRole('button', { name: /Sync from terminal/i }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => String(input).includes('/users/refresh') && init?.method === 'POST')).toBe(true))
    expect(await screen.findByText(/terminal user synchronization is queued/i)).toBeTruthy()
  })

  it('lets an ADD administrator confirm an initial terminal serial from the selected device', async () => {
    const awaitingDevice: Device = {
      ...device,
      state: 'OFFLINE',
      connected: false,
      zkt: {
        ...device.zkt!,
        serial: 'ANM4261301077',
        expected_serial: null,
        confirmed_serial: null,
        terminal_binding_state: 'SERIAL_CONFIRMATION_REQUIRED',
        certification_state: 'READ_ONLY',
        writes_disabled_reason: 'TERMINAL_SERIAL_CONFIRMATION_REQUIRED',
        online: false,
        connection_state: 'OFFLINE',
        capabilities: { create_user: false, user_write: false, admin_lease: false, delete_user: false },
      },
    }
    const pendingDevice: Device = {
      ...awaitingDevice,
      zkt: {
        ...awaitingDevice.zkt!,
        expected_serial: 'ANM4261301077',
        confirmed_serial: 'ANM4261301077',
        terminal_binding_state: 'PENDING_DEVICE_ACK',
        writes_disabled_reason: 'TERMINAL_SERIAL_PENDING_DEVICE_ACK',
      },
    }
    const baseFetch = workspaceFetch({ selectedDevice: awaitingDevice })
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/terminal-binding/confirm') && init?.method === 'POST') {
        return Promise.resolve(response({
          device: pendingDevice,
          command: { ...command, type: 'PIN_TERMINAL_SERIAL', status: 'WAITING_FOR_DEVICE' },
        }, 202))
      }
      return baseFetch(input, init)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<UsersHarness selectedDevice={awaitingDevice} />)

    expect(await screen.findByRole('heading', { name: 'Confirm this physical terminal' })).toBeTruthy()
    expect(screen.getByText('ANM4261301077')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Confirm terminal serial/i }))
    expect(screen.getByRole('heading', { name: 'Confirm physical terminal' })).toBeTruthy()
    expect(screen.getByText(/authorization remains queued for up to 10 minutes/i)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('ADD administrator password'), { target: { value: 'correct-password' } })
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm terminal serial' }))

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => {
      if (!String(input).includes('/terminal-binding/confirm') || init?.method !== 'POST') return false
      const body = JSON.parse(String(init.body))
      return body.observed_serial === 'ANM4261301077'
        && body.password === 'correct-password'
        && String(body.idempotency_key).startsWith('terminal-serial-confirmation:')
    })).toBe(true))
    expect(await screen.findByRole('heading', { name: 'Waiting for device acknowledgement' })).toBeTruthy()
    expect(screen.getByText(/will continue when the ADD device reconnects/i)).toBeTruthy()
  })

  it('ignores a superseded directory response and requires an actual edit before enabling submit', async () => {
    let resolveSlow: ((value: Response) => void) | null = null
    const fetchMock = workspaceFetch()
    const routedFetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://add.test')
      if (url.pathname.includes('/api/v2/devices/connector-one/users') && url.searchParams.get('q') === 'slow') {
        return new Promise<Response>((resolve) => { resolveSlow = resolve })
      }
      if (url.pathname.includes('/api/v2/devices/connector-one/users') && url.searchParams.get('q') === 'fast') {
        return Promise.resolve(response({
          rows: [user({ display_name: 'Fast Result' })], next_cursor: null, device,
          identity_integrity: { source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT', total_users: 1, with_cnic: 1, missing_cnic: 0, duplicate_groups: 0, duplicate_users: 0, resolved_duplicate_groups: 0, unresolved_duplicate_groups: 0, unresolved_duplicate_users: 0 },
        }))
      }
      return fetchMock(input, init)
    })
    vi.stubGlobal('fetch', routedFetch)
    render(<UsersHarness />)
    await screen.findByRole('article', { name: /Ayesha Khan/i })
    const search = screen.getByLabelText(/Search user name/i)
    fireEvent.change(search, { target: { value: 'slow' } })
    await waitFor(() => expect(resolveSlow).toBeTruthy())
    fireEvent.change(search, { target: { value: 'fast' } })
    expect(await screen.findByRole('article', { name: /Fast Result/i })).toBeTruthy()
    await act(async () => resolveSlow?.(response({
      rows: [user({ display_name: 'Stale Result' })], next_cursor: null, device,
      identity_integrity: { source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT', total_users: 1, with_cnic: 1, missing_cnic: 0, duplicate_groups: 0, duplicate_users: 0, resolved_duplicate_groups: 0, unresolved_duplicate_groups: 0, unresolved_duplicate_users: 0 },
    })))
    expect(screen.queryByRole('article', { name: /Stale Result/i })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Edit Fast Result/i }))
    fireEvent.change(screen.getByLabelText(/Confirm administrator password/i), { target: { value: 'password' } })
    expect((screen.getByRole('button', { name: 'Confirm operation' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText(/Full canonical name/i), { target: { value: 'Fast Result Updated' } })
    expect((screen.getByRole('button', { name: 'Confirm operation' }) as HTMLButtonElement).disabled).toBe(false)
  })
})
