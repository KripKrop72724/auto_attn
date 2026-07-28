import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App, {
  buildMachinePreview,
  bulkDeletionConfirmation,
  CommandProgress,
  confirmationMatches,
  formatAlertDiagnostics,
  identityConflictText,
  StatusBadge,
  validateUserDraft,
} from './App'
import { api } from './api'
import type { Device, DeviceUser } from './types'

const maskedCnic = '*****-****567-1'
const fullCnic = '3520212345671'

const device: Device = {
  connector_id: 'connector-one',
  hardware_id: 'e0:72:a1:d6:f3:28',
  zone_id: 'ZONE-SLICTOWER-3FL',
  zone_name: 'ZONE-SLICTOWER-3FL',
  device_id: '1',
  display_name: 'SLICTOWER · 3rd Floor',
  state: 'ONLINE',
  connected: true,
  firmware_version: '2.1.0',
  onboarding_generation: 2,
  last_onboarded_at: '2026-07-13T12:00:00Z',
  last_seen_at: '2026-07-13T12:01:00Z',
  current_activity: 'Live capture',
  last_error_code: null,
  zkt: {
    id: 1,
    serial: 'ADZV211860253',
    expected_serial: 'ADZV211860253',
    ip_address: '192.168.110.137',
    model: 'MB20/ID',
    platform: 'ZLM60_TFT',
    online: true,
    connection_state: 'ONLINE',
    consecutive_failures: 0,
    consecutive_successes: 4,
    flap_count_15m: 0,
    last_transition_at: null,
    last_online_at: null,
    offline_since: null,
    stability_since: null,
    backoff_until: null,
    probe_latency_ms: 12,
    certification_state: 'CERTIFIED',
    certification_observations: 2,
    capabilities: { user_write: true, create_user: true, delete_user: true },
    snapshot_complete: true,
    writes_disabled_reason: null,
    user_count: 1,
    attendance_count: 42,
    device_time: '2026-07-13T12:01:00Z',
    device_time_sampled_at: '2026-07-13T12:01:00Z',
    drift_seconds: 0,
    last_reconcile_at: '2026-07-13T12:00:00Z',
    next_restart_at: '2026-07-13T22:00:00Z',
  },
}

const user: DeviceUser = {
  id: 1,
  user_key: 'test-user-key',
  uid: '7',
  user_id: '1007',
  display_name: 'Ayesha Fatima',
  cnic_masked: maskedCnic,
  cnic_available: true,
  identity_complete: true,
  identity_conflict_code: null,
  identity_conflict_members: [],
  identity_conflict_resolved: false,
  identity_resolution_id: null,
  shift_worker: false,
  privilege: 0,
  present: true,
  lifecycle_state: 'ACTIVE',
  row_version: 3,
  observed_at: '2026-07-13T12:00:00Z',
  machine_name_preview: 'Ayesha-*****-****567-1',
  current_command_state: null,
  read_only: false,
}

const conflictedUser: DeviceUser = {
  ...user,
  identity_complete: false,
  identity_conflict_code: 'DUPLICATE_CNIC',
  identity_conflict_members: [{ user_id: '1008', uid: '8' }],
}

const deliveryAlert = {
  id: 9,
  code: 'ORDS_DELIVERY_FAILED',
  severity: 'WARNING',
  state: 'OPEN',
  message: 'Oracle attendance delivery is retrying; preserved events remain queued.',
  details: {
    failure_category: 'HTTP_503',
    http_status: 503,
    attempt_count: 4,
    response_body: `must-not-render-${fullCnic}`,
  },
  first_seen_at: '2026-07-13T12:00:00Z',
  last_seen_at: '2026-07-13T12:05:00Z',
  acknowledged_at: null,
  resolved_at: null,
}

class EventSourceStub {
  onmessage: ((event: MessageEvent) => void) | null = null
  constructor(_url: string, _options?: EventSourceInit) {}
  addEventListener() {}
  close() {}
}

const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })

const fetchStub = (users: DeviceUser[] = [user]) =>
  vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path.includes('/api/v1/auth/session')) {
      return response({ username: 'StateHealthAdmin', csrf_token: 'csrf' })
    }
    if (path.includes('/api/v1/overview')) {
      return response({
        total: 1,
        online: 1,
        open_alerts: 1,
        active_leases: 0,
        ords_delivery: {
          backlog: 12,
          pending: 9,
          retrying: 3,
          in_flight: 0,
          blocked_identity: 2,
          quarantined: 1,
          acknowledged: 24,
          acknowledged_add: 24,
          acknowledged_check: 0,
          acknowledged_firmware: 0,
          firmware_unverified: 0,
          membership_reverify: 0,
          oldest_backlog_at: '2026-07-13T12:00:00Z',
          last_attempt_at: '2026-07-13T12:05:00Z',
        },
      })
    }
    if (path.endsWith('/api/v1/firmware/releases')) {
      return response({
        enabled: false,
        hil_enabled: true,
        rows: [{
          release_id: 'release-2-2-4',
          version: '2.2.4',
          git_sha: 'f11f9306d7c0f01df19123dda9bea6157a68119d',
          image_sha256: 'a'.repeat(64),
          application_sha256: 'b'.repeat(64),
          image_size: 1_048_576,
          state: 'HIL_ONLY',
          partition_layout: 'ota_0/ota_1',
          signing_key_id: 'zone-lite-production',
          published_at: '2026-07-27T12:00:00Z',
          hil_target_mac: device.hardware_id,
        }],
      })
    }
    if (path.endsWith('/api/v1/firmware/campaigns') && init?.method === 'POST') {
      return response({
        campaign_id: 'campaign-one',
        status: 'ACTIVE',
        eligible: 1,
        legacy_skipped: 0,
      }, 201)
    }
    if (path.endsWith('/api/v1/firmware/campaigns')) {
      return response({ enabled: false, hil_enabled: true, rows: [] })
    }
    if (/\/api\/v1\/firmware\/campaigns\/[^/]+\/(pause|resume|cancel)$/.test(path)) {
      return response({ campaign_id: 'campaign-one', status: 'PAUSED' })
    }
    if (path.includes('/logs?')) return response({ rows: [] })
    if (path.includes('/connectivity?')) return response({ rows: [] })
    if (path.includes('/alerts')) return response({ rows: [deliveryAlert] })
    if (path.endsWith('/api/v1/devices/connector-one')) return response(device)
    if (path.includes('/api/v1/devices') && !path.includes('/users')) {
      return response({ rows: [device] })
    }
    if (path.includes('/api/v2/devices/connector-one/identity-conflicts')) {
      const hasConflict = users.some((row) => row.identity_conflict_code)
      return response({
        evidence_scope: {
          snapshot_source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT',
          terminal_attendance_count: 42,
          add_attendance_count: 4,
          attendance_coverage_percent: 9.52,
          attendance_is_immutable: true,
          terminal_users_are_unchanged: true,
        },
        raw_duplicate_groups: hasConflict ? 1 : 0,
        resolved_groups: 0,
        unresolved_groups: hasConflict ? 1 : 0,
        groups: hasConflict
          ? [{
              group_token: 'a'.repeat(64),
              cnic_masked: maskedCnic,
              classification: 'POSSIBLE_NAME_VARIANT',
              status: 'UNRESOLVED',
              resolution_id: null,
              resolution_created_at: null,
              resolution_reason: null,
              recommended_action: 'HR_IDENTITY_REVIEW',
              members: [
                {
                  user_key: conflictedUser.user_key,
                  uid: conflictedUser.uid,
                  user_id: conflictedUser.user_id,
                  display_name: conflictedUser.display_name,
                  row_version: conflictedUser.row_version,
                  privilege: conflictedUser.privilege,
                  observed_at: conflictedUser.observed_at,
                  punch_evidence: { captured_count: 4, first_captured_at: null, last_captured_at: null, blocked_identity_count: 1 },
                },
                {
                  user_key: 'other-user-key',
                  uid: '8',
                  user_id: '1008',
                  display_name: 'Ayesha F.',
                  row_version: 2,
                  privilege: 0,
                  observed_at: conflictedUser.observed_at,
                  punch_evidence: { captured_count: 0, first_captured_at: null, last_captured_at: null, blocked_identity_count: 0 },
                },
              ],
            }]
          : [],
      })
    }
    if (path.includes('/api/v2/devices/connector-one/user-deletion-jobs/latest')) {
      return response({ job: null })
    }
    if (path.includes('/api/v2/devices/connector-one/users')) {
      const hasConflict = users.some((row) => row.identity_conflict_code)
      return response({
        rows: users,
        next_cursor: null,
        device,
        identity_integrity: {
          source: 'CURRENT_COMPLETE_ZKT_SNAPSHOT',
          total_users: hasConflict ? 2 : users.length,
          with_cnic: hasConflict ? 2 : users.length,
          missing_cnic: 0,
          duplicate_groups: hasConflict ? 1 : 0,
          duplicate_users: hasConflict ? 2 : 0,
          resolved_duplicate_groups: 0,
          unresolved_duplicate_groups: hasConflict ? 1 : 0,
          unresolved_duplicate_users: hasConflict ? 2 : 0,
        },
      })
    }
    return response({ rows: [] })
  })

describe('State Life ADD interface', () => {
  beforeEach(() => {
    vi.stubGlobal('EventSource', EventSourceStub)
    vi.stubGlobal('fetch', fetchStub())
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('builds an exact device-scoped bulk deletion confirmation', () => {
    expect(bulkDeletionConfirmation(12, 'ZONE-SWAT-01')).toBe(
      'DELETE 12 USERS FROM ZONE-SWAT-01',
    )
  })

  it('uses secure automatic onboarding language and exposes no registration control', async () => {
    render(<App />)
    expect(await screen.findByRole('heading', { name: /attendance device command center/i })).toBeTruthy()
    expect(screen.getByAltText('State Life Insurance Corporation')).toBeTruthy()
    expect(screen.queryByText(/register connector/i)).toBeNull()
    expect(screen.getByText(/secure auto-onboarding enabled/i)).toBeTruthy()
    expect(screen.getByText('ORDS delivery queue')).toBeTruthy()
    expect(await screen.findByText('12')).toBeTruthy()
    expect(await screen.findByText(/3 retrying · 2 identity blocked · 1 quarantined/i)).toBeTruthy()
  })

  it('renders only masked CNIC in the selected-terminal users workspace', async () => {
    render(<App />)
    await screen.findByRole('heading', { name: /attendance device command center/i })
    fireEvent.click(screen.getByRole('button', { name: 'Users' }))
    await screen.findByRole('option', { name: /SLICTOWER · 3rd Floor · ADZV211860253/i })
    fireEvent.change(screen.getByLabelText('Selected terminal'), {
      target: { value: device.connector_id },
    })
    expect(await screen.findByText(maskedCnic)).toBeTruthy()
    expect(document.body.textContent).not.toContain(fullCnic)
    expect(screen.getByRole('button', { name: /edit ayesha fatima/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /delete ayesha fatima/i })).toBeTruthy()
  })

  it('marks duplicate CNIC identities with text and a non-color-only pattern', async () => {
    vi.stubGlobal('fetch', fetchStub([conflictedUser]))
    render(<App />)
    await screen.findByRole('heading', { name: /attendance device command center/i })
    fireEvent.click(screen.getByRole('button', { name: 'Users' }))
    fireEvent.change(await screen.findByLabelText('Selected terminal'), {
      target: { value: device.connector_id },
    })
    const warning = await screen.findByText(/exact cnic also encoded on user 1008 \(uid 8\)/i)
    expect(warning.closest('article')?.classList.contains('identity-conflict')).toBe(true)
    expect(screen.getByText(/2 unresolved users across 1 exact-cnic groups/i)).toBeTruthy()
    expect(screen.getByRole('option', { name: 'CNIC conflict' })).toBeTruthy()
    expect(document.body.textContent).not.toContain(fullCnic)
    expect(identityConflictText(conflictedUser)).toContain('1008 (UID 8)')
    fireEvent.click(screen.getByRole('button', { name: /review same-employee alias/i }))
    expect(screen.getByRole('heading', { name: /verify same employee/i })).toBeTruthy()
    expect(screen.getByText(/no zkt user, fingerprint template, uid, or attendance event is merged/i)).toBeTruthy()
    expect(screen.getByLabelText(/type “same employee”/i)).toBeTruthy()
    expect(screen.getByLabelText(/confirm administrator password/i)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /close dialog/i }))
    fireEvent.click(screen.getByRole('button', { name: /edit ayesha fatima/i }))
    expect(
      screen.getByLabelText(/replacement cnic \(required to resolve conflict\)/i),
    ).toBeTruthy()
  })

  it('communicates every state with text, icon, and border pattern', () => {
    const { container, rerender } = render(<StatusBadge state="ONLINE" />)
    expect(container.querySelector('[data-pattern="confirmed"]')).toBeTruthy()
    expect(container.querySelector('svg')).toBeTruthy()
    expect(screen.getByText('ONLINE')).toBeTruthy()
    rerender(<StatusBadge state="WAITING_FOR_ZKT" />)
    expect(container.querySelector('[data-pattern="waiting"]')).toBeTruthy()
    expect(screen.getByText('WAITING FOR ZKT')).toBeTruthy()
  })

  it('renders an unknown status instead of crashing on a partial API response', () => {
    const { container } = render(<StatusBadge state={undefined} />)
    expect(screen.getByText('UNKNOWN')).toBeTruthy()
    expect(container.querySelector('[data-pattern="notice"]')).toBeTruthy()
  })

  it('starts an exact-target HIL firmware campaign with step-up confirmation', async () => {
    const fetchMock = fetchStub()
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await screen.findByRole('heading', { name: /attendance device command center/i })
    fireEvent.click(screen.getByRole('button', { name: 'Firmware' }))
    expect(await screen.findByRole('heading', { name: 'Firmware releases and campaigns' })).toBeTruthy()
    expect(await screen.findByText(device.hardware_id)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Signed release'), {
      target: { value: 'release-2-2-4' },
    })
    expect((screen.getByLabelText('Zone') as HTMLSelectElement).value).toBe(device.zone_id)
    expect((screen.getByLabelText('Zone') as HTMLSelectElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Audited reason'), {
      target: { value: 'Pilot SWAT deployment validation' },
    })
    fireEvent.change(screen.getByLabelText('Type release version to confirm'), {
      target: { value: '2.2.4' },
    })
    const password = screen.getByLabelText('Administrator password') as HTMLInputElement
    fireEvent.change(password, { target: { value: 'local-step-up-value' } })
    fireEvent.click(screen.getByRole('button', { name: /start firmware campaign/i }))

    expect(await screen.findByText(/campaign created for 1 eligible device/i)).toBeTruthy()
    const campaignCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        String(input).endsWith('/api/v1/firmware/campaigns') && init?.method === 'POST',
    )
    expect(campaignCall).toBeTruthy()
    expect(JSON.parse(String(campaignCall?.[1]?.body))).toEqual({
      release_id: 'release-2-2-4',
      zone_id: device.zone_id,
      reason: 'Pilot SWAT deployment validation',
      typed_confirmation: '2.2.4',
      password: 'local-step-up-value',
    })
    expect(password.value).toBe('')
  })

  it('returns to the login screen when any request reports an expired session', async () => {
    render(<App />)
    await screen.findByRole('heading', { name: /attendance device command center/i })
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
    expect(screen.getByRole('heading', { name: 'UNKNOWN' })).toBeTruthy()
    expect(screen.getByText(/durably tracked/i)).toBeTruthy()
  })

  it('renders only allowlisted alert diagnostics', async () => {
    render(<App />)
    await screen.findByRole('heading', { name: /attendance device command center/i })
    fireEvent.click(screen.getByRole('button', { name: /alerts/i }))
    expect(await screen.findByText(/Category HTTP_503 · HTTP 503 · Attempt 4/i)).toBeTruthy()
    expect(document.body.textContent).not.toContain('must-not-render')
    expect(document.body.textContent).not.toContain(fullCnic)
    expect(formatAlertDiagnostics({ failure_category: 'unsafe detail!', secret: 'x' })).toBe('')
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
    expect(screen.getByRole('heading', { name: 'WAITING FOR ZKT' })).toBeTruthy()
    expect(screen.getByText(/durably tracked/i)).toBeTruthy()
    expect(container.querySelector('.pattern-waiting')).toBeTruthy()
    expect(screen.getByRole('button', { name: /cancel before execution/i })).toBeTruthy()
  })

  it('supports semantic keyboard tabs, Escape close, and focus restoration', async () => {
    render(<App />)
    await screen.findByRole('heading', { name: /attendance device command center/i })
    const inspect = await screen.findByRole('button', { name: `Inspect ${device.display_name}` })
    inspect.focus()
    fireEvent.click(inspect)
    expect(await screen.findByRole('dialog')).toBeTruthy()
    const overview = screen.getByRole('tab', { name: 'overview' })
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

describe('user-operation safety rules', () => {
  it('builds a valid UTF-8 24-byte machine-name projection', () => {
    const preview = buildMachinePreview('زارا State Life Employee', fullCnic, true)
    expect(new TextEncoder().encode(preview).length).toBeLessThanOrEqual(24)
    expect(preview.endsWith(`-S-${fullCnic}`)).toBe(true)
  })

  it('validates CNIC, numeric override, password, and typed deletion confirmation', () => {
    expect(
      validateUserDraft({
        displayName: 'Ayesha',
        cnic: '123',
        password: 'secret',
      }),
    ).toMatch(/13 digits/)
    expect(
      validateUserDraft({
        displayName: 'Ayesha',
        cnic: fullCnic,
        password: 'secret',
        userIdOverride: 'employee-seven',
      }),
    ).toMatch(/numeric/)
    expect(confirmationMatches('Ayesha Fatima', user)).toBe(true)
    expect(confirmationMatches('1007', user)).toBe(true)
    expect(confirmationMatches('Ayesha', user)).toBe(false)
  })
})
