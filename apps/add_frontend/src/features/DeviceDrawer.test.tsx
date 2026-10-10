import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { dateTime, useToast } from '../App'
import type { Alert, CommKeyState, Device, DeviceHealth, HealthReason } from '../types'
import { DeviceDrawer } from './DeviceDrawer'
import { deviceSnapshots } from '../deviceData'


const device: Device = {
  connector_id: 'connector-quetta',
  hardware_id: '00:17:61:12:03:32',
  zone_id: 'QUETTA',
  zone_name: 'Quetta',
  device_id: '1',
  display_name: 'Quetta device',
  state: 'ONLINE',
  connected: true,
  firmware_version: 'zone-lite-2.5.0',
  comm_key_capable: true,
  comm_key_revision: 1,
  onboarding_generation: 1,
  last_onboarded_at: null,
  last_seen_at: '2026-08-25T05:00:00Z',
  current_activity: 'ONLINE',
  last_error_code: null,
  zkt: null,
}

const state: CommKeyState = {
  enabled: true,
  reveal_enabled: true,
  management_state: 'APPLIED',
  applied_revision: 1,
  desired_revision: 1,
  last_verified_at: '2026-08-25T05:00:00Z',
  verified_terminal_serial: 'UFS2253100068',
  last_error_code: null,
  managed: true,
  capabilities: {
    esp_only: true,
    esp_and_terminal: false,
    esp_and_terminal_block_reason: 'TERMINAL_MODEL_NOT_CERTIFIED',
    recovery_staging: false,
  },
  active_operation: null,
}

const response = (body: unknown) => new Response(JSON.stringify(body), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
})

let activeDevice = device
let activeAlerts: Alert[] = []
let resolvedAlerts: Alert[] = []

const workerReason: HealthReason = {
  code: 'ESP_DELIVERY_WORKER_FAULT',
  error_code: 'ESP_DELIVERY_WORKER_FAULT',
  tier: 'WARNING',
  currency: 'PREVIOUS_BOOT',
  gating: true,
  severity: 'HIGH',
  message: 'Delivery worker add_delivery reported STOPPED.',
  since: '2026-10-09T08:00:00Z',
  last_seen_at: '2026-10-09T09:00:00Z',
  alert_id: 41,
  alert_state: 'OPEN',
  acknowledged_at: null,
  acknowledged_by: null,
  boot_id: '1a2b3c4d-0000-4000-8000-000000000001',
  firmware_version: '2.6.27',
  binding: 'INFERRED_PREVIOUS',
  latch: null,
  evidence_summary: 'add_delivery STOPPED · ords_delivery STOPPED',
  clear_condition: 'Clears when every required delivery worker reports running with a fresh tick.',
  operator: { resolvable: true, refusal_code: null, refusal: null },
}

const durabilityReason: HealthReason = {
  ...workerReason,
  code: 'ESP_DURABILITY_FAULT',
  error_code: 'ESP_DURABILITY_FAULT',
  tier: 'DEGRADED',
  currency: 'HELD',
  message: 'Storage durability is DEGRADED.',
  since: '2026-10-10T07:00:00Z',
  alert_id: 42,
  boot_id: '9f8e7d6c-0000-4000-8000-000000000002',
  firmware_version: '2.6.15',
  binding: 'OBSERVED',
  evidence_summary: 'durability DEGRADED · write failures 0',
  clear_condition: 'Clears when firmware that reports storage diagnostics verifies healthy, persisted and recovered storage.',
  operator: { resolvable: false, refusal_code: 'ALERT_CONDITION_CURRENT', refusal: 'Held on this boot until the device reports verified evidence.' },
}

const healthyLink = { state: 'CONNECTED', raw_state: 'ONLINE', since: '2026-10-10T06:00:00Z', reason: 'LINK_UP', message: 'The terminal link is up.' }

const fullHealth: DeviceHealth = {
  mode: 'ENFORCED',
  tier: 'DEGRADED',
  derived_lifecycle: 'DEGRADED',
  last_error_code: 'ESP_DURABILITY_FAULT',
  primary: { code: 'ESP_DURABILITY_FAULT', error_code: 'ESP_DURABILITY_FAULT', tier: 'DEGRADED', message: durabilityReason.message, since: durabilityReason.since },
  degraded_count: 1,
  warning_count: 1,
  evaluated_at: '2026-10-10T08:00:00Z',
  lifecycle_state: 'DEGRADED',
  reasons: [durabilityReason, workerReason],
  other_active_alerts: [],
  terminal_link: healthyLink,
  coverage: [
    { key: 'storage', label: 'Storage durability', status: 'REPORTED', detail: 'DEGRADED; persistence verified; recovery complete.' },
    { key: 'led', label: 'LED fault channel', status: 'ACTIVE', detail: 'Local storage or resource failures and fatal boot faults are reported through the LED state.' },
  ],
  device_error: { code: 'ESP_DURABILITY_FAULT', message: 'Storage durability is DEGRADED.', derived_code: 'ESP_DURABILITY_FAULT', backed: true, backing_alert_ids: [42] },
}

const alertRow = (overrides: Partial<Alert>): Alert => ({
  id: 41,
  code: 'ESP_DELIVERY_WORKER_FAULT',
  severity: 'HIGH',
  state: 'OPEN',
  message: 'Delivery worker add_delivery reported STOPPED.',
  details: { binding: 'INFERRED_PREVIOUS', firmware_version: '2.6.27' },
  first_seen_at: '2026-10-09T08:00:00Z',
  last_seen_at: '2026-10-09T09:00:00Z',
  acknowledged_at: null,
  acknowledged_by: null,
  resolved_at: null,
  resolution: null,
  ...overrides,
})

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const drawer = (seed: Device = activeDevice) => {
  const toast = { notice: vi.fn(), error: vi.fn() } as unknown as ReturnType<typeof useToast>
  const onClose = vi.fn()
  render(<DeviceDrawer seed={seed} revision={0} onClose={onClose} onManageUsers={vi.fn()} onInventoryChanged={vi.fn(async () => undefined)} toast={toast} />)
  return { toast, onClose }
}

describe('DeviceDrawer COMM Key controls', () => {
  beforeEach(() => {
    deviceSnapshots.clear()
    activeDevice = device
    activeAlerts = []
    resolvedAlerts = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.includes('/alerts?queue=ACTIVE')) return response({ rows: activeAlerts })
      if (path.includes('/alerts?queue=RESOLVED')) return response({ rows: resolvedAlerts })
      if (/\/api\/v1\/alerts\/\d+\/acknowledge$/.test(path) && init?.method === 'POST') {
        return response(alertRow({ acknowledged_at: '2026-10-10T08:05:00Z', acknowledged_by: 'StateHealthAdmin' }))
      }
      if (/\/api\/v1\/alerts\/\d+\/resolve$/.test(path) && init?.method === 'POST') {
        return response({
          alert: alertRow({ state: 'RESOLVED', resolved_at: '2026-10-10T08:06:00Z' }),
          device_state: 'DEGRADED',
          device_error: fullHealth.device_error,
          residual_alert_id: null,
        })
      }
      if (path.endsWith('/clear-error') && init?.method === 'POST') {
        return response({ device_state: 'ONLINE', device_error: { code: null, message: null, derived_code: null, backed: false, backing_alert_ids: [] } })
      }
      if (path.endsWith('/spare') && init?.method === 'PATCH') {
        activeDevice = { ...activeDevice, is_spare: JSON.parse(String(init.body)).spare }
        return response(activeDevice)
      }
      if (path.endsWith('/terminal-binding/replace') && init?.method === 'POST') {
        return response({ device: activeDevice, command: { command_id: 'replacement-command' } })
      }
      if (path.endsWith('/comm-key/reveal') && init?.method === 'POST') {
        return response({
          comm_key: '1979',
          applied_revision: 1,
          verified_terminal_serial: 'UFS2253100068',
          last_verified_at: '2026-08-25T05:00:00Z',
        })
      }
      if (path.endsWith('/comm-key')) return response(state)
      if (path.includes('/logs?') || path.includes('/connectivity?')) return response({ rows: [] })
      if (path.endsWith(`/devices/${device.connector_id}`)) return response(activeDevice)
      throw new Error(`Unexpected request: ${path}`)
    }))
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('shows the reported Hikvision cadence and measured interval', async () => {
    activeDevice = { ...device, firmware_family: 'hikvision', hikvision: {
      capture_mode: 'poll', poll_interval_seconds: 2, last_poll_interval_ms: 2150,
      qualification_state: 'NOT_QUALIFIED', poll_error: 0, source_queue_depth: 0,
    } }
    const toast = { notice: vi.fn(), error: vi.fn() } as unknown as ReturnType<typeof useToast>
    render(<DeviceDrawer seed={activeDevice} revision={0} onClose={vi.fn()}
      onManageUsers={vi.fn()} onInventoryChanged={vi.fn()} toast={toast} />)
    expect(await screen.findByText('2-second polling')).toBeTruthy()
    expect(screen.getByText('2.1 seconds')).toBeTruthy()
    expect(screen.getByText('HIKVISION TERMINAL')).toBeTruthy()
    expect(screen.getByText(/Live polling continues during reconciliation/)).toBeTruthy()
  })

  it('requires the break-glass workflow and hides the revealed key on blur', async () => {
    const toast = { notice: vi.fn(), error: vi.fn() } as unknown as ReturnType<typeof useToast>
    render(
      <DeviceDrawer
        seed={device}
        revision={0}
        onClose={vi.fn()}
        onManageUsers={vi.fn()}
        onInventoryChanged={vi.fn()}
        toast={toast}
      />,
    )

    fireEvent.click(await screen.findByRole('tab', { name: 'Controls' }))
    await screen.findByText('Break-glass reveal')
    fireEvent.change(screen.getByLabelText('Reveal reason'), {
      target: { value: 'Authorized Quetta recovery credential inspection' },
    })
    fireEvent.change(screen.getByLabelText(/Type REVEAL connector-quetta/), {
      target: { value: 'REVEAL connector-quetta' },
    })
    fireEvent.change(screen.getAllByLabelText('Confirm administrator password')[1], {
      target: { value: 'correct-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Reveal for 15 seconds' }))

    expect(await screen.findByText('1979')).not.toBeNull()
    fireEvent.blur(window)
    await waitFor(() => expect(screen.queryByText('1979')).toBeNull())
  })

  it('requires exact old/new serial evidence for an authenticated terminal replacement', async () => {
    activeDevice = {
      ...device,
      state: 'DEGRADED',
      zkt: {
        id: 7,
        serial: 'CKPG221260408',
        expected_serial: 'AEH2232460004',
        confirmed_serial: 'AEH2232460004',
        terminal_binding_state: 'CONFIRMED',
        serial_confirmed_by: 'StateHealthAdmin',
        serial_confirmed_at: '2026-08-27T05:00:00Z',
        ip_address: '192.168.1.250',
        model: 'uFace800 Plus/ID',
        platform: 'ZMM720_TFT',
        online: false,
        connection_state: 'ONLINE',
        consecutive_failures: 0,
        consecutive_successes: 1,
        flap_count_15m: 0,
        last_transition_at: '2026-08-29T08:00:00Z',
        last_online_at: '2026-08-29T08:00:00Z',
        offline_since: null,
        stability_since: null,
        backoff_until: null,
        probe_latency_ms: 5,
        certification_state: 'READ_ONLY',
        certification_observations: 0,
        capabilities: {},
        snapshot_complete: false,
        writes_disabled_reason: 'SERIAL_MISMATCH',
        user_count: 48,
        attendance_count: 0,
        device_time: null,
        device_time_sampled_at: null,
        drift_seconds: null,
        last_reconcile_at: null,
        next_restart_at: null,
      },
    }
    const toast = { notice: vi.fn(), error: vi.fn() } as unknown as ReturnType<typeof useToast>
    render(
      <DeviceDrawer
        seed={activeDevice}
        revision={0}
        onClose={vi.fn()}
        onManageUsers={vi.fn()}
        onInventoryChanged={vi.fn()}
        toast={toast}
      />,
    )

    fireEvent.click(await screen.findByRole('tab', { name: 'Controls' }))
    const card = screen.getByRole('heading', { name: 'Replace terminal binding' }).closest('article')
    expect(card).not.toBeNull()
    const controls = within(card as HTMLElement)
    fireEvent.change(controls.getByLabelText(/Type REPLACE connector-quetta/), {
      target: { value: 'REPLACE connector-quetta AEH2232460004 CKPG221260408' },
    })
    fireEvent.change(controls.getByLabelText('Confirm administrator password'), {
      target: { value: 'correct-password' },
    })
    fireEvent.click(controls.getByRole('button', { name: 'Replace binding' }))

    await waitFor(() => expect(toast.notice).toHaveBeenCalled())
    const request = vi.mocked(fetch).mock.calls.find(([input]) =>
      String(input).endsWith('/terminal-binding/replace'))
    expect(request).toBeTruthy()
    expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({
      current_serial: 'AEH2232460004',
      observed_serial: 'CKPG221260408',
      typed_confirmation: 'REPLACE connector-quetta AEH2232460004 CKPG221260408',
    })
  })

  it('moves a device into and out of spare inventory from the overview', async () => {
    const toast = { notice: vi.fn(), error: vi.fn() } as unknown as ReturnType<typeof useToast>
    const onInventoryChanged = vi.fn(async () => undefined)
    render(
      <DeviceDrawer
        seed={device}
        revision={0}
        onClose={vi.fn()}
        onManageUsers={vi.fn()}
        onInventoryChanged={onInventoryChanged}
        toast={toast}
      />,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Move to spare inventory' }))
    await screen.findByText(/excluded from fleet health and alerts/i)
    expect(screen.getByRole('button', { name: 'Return to active fleet' })).toBeTruthy()
    expect(onInventoryChanged).toHaveBeenCalledTimes(1)
    expect(toast.notice).toHaveBeenCalledWith(expect.stringMatching(/moved to spare inventory/i))

    fireEvent.click(screen.getByRole('button', { name: 'Return to active fleet' }))
    await screen.findByRole('button', { name: 'Move to spare inventory' })
    expect(onInventoryChanged).toHaveBeenCalledTimes(2)
  })
})

describe('DeviceDrawer device health', () => {
  beforeEach(() => {
    deviceSnapshots.clear()
    activeDevice = { ...device, state: 'DEGRADED', last_error_code: 'ESP_DURABILITY_FAULT', terminal_link: healthyLink, health: fullHealth }
    activeAlerts = []
    resolvedAlerts = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.includes('/alerts?queue=ACTIVE')) return response({ rows: activeAlerts })
      if (path.includes('/alerts?queue=RESOLVED')) return response({ rows: resolvedAlerts })
      if (/\/api\/v1\/alerts\/\d+\/acknowledge$/.test(path) && init?.method === 'POST') {
        return response(alertRow({ acknowledged_at: '2026-10-10T08:05:00Z', acknowledged_by: 'StateHealthAdmin' }))
      }
      if (/\/api\/v1\/alerts\/\d+\/resolve$/.test(path) && init?.method === 'POST') {
        return response({
          alert: alertRow({ state: 'RESOLVED', resolved_at: '2026-10-10T08:06:00Z' }),
          device_state: 'DEGRADED',
          device_error: fullHealth.device_error,
          residual_alert_id: null,
        })
      }
      if (path.endsWith('/clear-error') && init?.method === 'POST') {
        return response({ device_state: 'ONLINE', device_error: { code: null, message: null, derived_code: null, backed: false, backing_alert_ids: [] } })
      }
      if (path.endsWith('/comm-key')) return response(state)
      if (path.includes('/logs?') || path.includes('/connectivity?')) return response({ rows: [] })
      if (path.endsWith(`/devices/${device.connector_id}`)) return response(activeDevice)
      throw new Error(`Unexpected request: ${path}`)
    }))
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('explains the derived tier with reasons instead of an active problem card', async () => {
    drawer()
    expect(await screen.findByRole('heading', { name: 'Why this device is degraded' })).toBeTruthy()
    expect(screen.queryByText('ACTIVE PROBLEM')).toBeNull()
    // The header and the panel's terminal-link card both show the link.
    expect(screen.getAllByLabelText('Terminal link: Connected')).toHaveLength(2)

    const durability = screen.getByRole('listitem', { name: 'ESP durability fault' })
    expect(within(durability).getByText('Degraded')).toBeTruthy()
    expect(within(durability).getByText('Held: waiting for verified evidence')).toBeTruthy()
    expect(within(durability).getByText('Since')).toBeTruthy()
    expect(within(durability).getByText(new RegExp(escapeRegExp(dateTime(durabilityReason.since))))).toBeTruthy()
    expect(within(durability).getByText(durabilityReason.clear_condition as string)).toBeTruthy()
    expect(within(durability).getByText('durability DEGRADED · write failures 0')).toBeTruthy()
    const blockedResolve = within(durability).getByRole('button', { name: 'Resolve with reason' }) as HTMLButtonElement
    expect(blockedResolve.disabled).toBe(true)
    const refusal = within(durability).getByText('Held on this boot until the device reports verified evidence.')
    expect(blockedResolve.getAttribute('aria-describedby')).toBe(refusal.id)

    const worker = screen.getByRole('listitem', { name: 'ESP delivery worker fault' })
    expect(within(worker).getByText('Warning')).toBeTruthy()
    expect(within(worker).getByText('From an earlier boot (firmware 2.6.27, boot 1a2b3c4d…)')).toBeTruthy()
    expect((within(worker).getByRole('button', { name: 'Resolve with reason' }) as HTMLButtonElement).disabled).toBe(false)
    expect(within(worker).getByRole('button', { name: 'Acknowledge' })).toBeTruthy()

    // A backed device error can only clear with its alert.
    expect(screen.getByText(/Backed by active alert #42/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Re-evaluate device error' })).toBeNull()
    expect(screen.getByText('What ADD can verify on this firmware')).toBeTruthy()
  })

  it('labels the derived model as a preview in shadow mode', async () => {
    activeDevice = {
      ...activeDevice,
      state: 'DEGRADED',
      health: { ...fullHealth, mode: 'SHADOW', tier: 'ONLINE_WITH_WARNINGS', derived_lifecycle: 'ONLINE_WITH_WARNINGS', reasons: [workerReason] },
    }
    drawer()
    expect(await screen.findByRole('heading', { name: 'New health model (preview): Online with warnings' })).toBeTruthy()
    const panel = screen.getByRole('article', { name: 'New health model (preview): Online with warnings' })
    expect(within(panel).getByText('Current status')).toBeTruthy()
    expect(within(panel).getByLabelText('Status: DEGRADED')).toBeTruthy()
    expect(within(panel).getByLabelText('Status: ONLINE WITH WARNINGS').getAttribute('data-pattern')).toBe('notice')
  })

  it('re-evaluates an unbacked device error with step-up and the expected code', async () => {
    activeDevice = {
      ...activeDevice,
      state: 'ONLINE',
      last_error_code: 'ZKT_CONNECTION_FLAPPING',
      health: {
        ...fullHealth,
        tier: 'ONLINE',
        derived_lifecycle: 'ONLINE',
        last_error_code: null,
        primary: null,
        degraded_count: 0,
        warning_count: 0,
        reasons: [],
        device_error: { code: 'ZKT_CONNECTION_FLAPPING', message: 'Terminal connection is flapping.', derived_code: null, backed: false, backing_alert_ids: [] },
      },
    }
    const { toast } = drawer()
    fireEvent.click(await screen.findByRole('button', { name: 'Re-evaluate device error' }))
    const dialog = await screen.findByRole('dialog', { name: 'Re-evaluate device error' })
    const submit = within(dialog).getByRole('button', { name: 'Re-evaluate device error' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    fireEvent.change(within(dialog).getByLabelText('Audited reason'), { target: { value: 'Flapping cleared after the cable was replaced.' } })
    fireEvent.change(within(dialog).getByLabelText('Administrator password'), { target: { value: 'local-step-up' } })
    expect(submit.disabled).toBe(false)
    fireEvent.click(submit)
    await waitFor(() => expect(toast.notice).toHaveBeenCalledWith(expect.stringMatching(/audit entry/)))
    const request = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith('/clear-error'))
    expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({
      expected_code: 'ZKT_CONNECTION_FLAPPING',
      reason: 'Flapping cleared after the cable was replaced.',
      password: 'local-step-up',
      idempotency_key: expect.stringMatching(/^device-error-clear:/),
    })
    expect(screen.queryByRole('dialog', { name: 'Re-evaluate device error' })).toBeNull()
  })

  it('lists device alerts by queue and resolves one with an audited reason', async () => {
    activeAlerts = [
      alertRow({}),
      alertRow({
        id: 43,
        code: 'ZKT_CLOCK_DRIFT',
        severity: 'WARNING',
        message: 'Terminal clock drift exceeds two minutes.',
        details: { acknowledged_by: 'StateHealthAdmin', acknowledgement_note: 'Clock reset scheduled' },
        acknowledged_at: '2026-10-10T07:30:00Z',
        acknowledged_by: 'StateHealthAdmin',
      }),
    ]
    resolvedAlerts = [alertRow({
      id: 40,
      state: 'RESOLVED',
      message: 'Delivery worker ords_delivery reported STOPPED.',
      resolved_at: '2026-10-10T06:00:00Z',
      details: { resolution: { kind: 'OPERATOR', actor: 'StateHealthAdmin', reason: 'Stranded by the 2.6.27 recovery image.', at: '2026-10-10T06:00:00Z' } },
      resolution: { kind: 'OPERATOR', actor: 'StateHealthAdmin', reason: 'Stranded by the 2.6.27 recovery image.', at: '2026-10-10T06:00:00Z' },
    })]
    const { toast, onClose } = drawer()
    fireEvent.click(await screen.findByRole('tab', { name: /^Alerts/ }))
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).endsWith(`/devices/${device.connector_id}/alerts?queue=ACTIVE&limit=200`))).toBe(true)
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).endsWith(`/devices/${device.connector_id}/alerts?queue=RESOLVED&limit=20`))).toBe(true)

    const needsAction = await screen.findByRole('region', { name: /Needs action/ })
    const acknowledged = screen.getByRole('region', { name: /Acknowledged/ })
    const resolved = screen.getByRole('region', { name: 'Recently resolved' })
    expect(within(needsAction).getByRole('heading', { name: workerReason.message })).toBeTruthy()
    const acknowledgedCard = within(acknowledged).getByRole('article', { name: 'Terminal clock drift exceeds two minutes.' })
    expect(acknowledgedCard.className).toContain('pattern-notice')
    expect(within(acknowledgedCard).getByText(/Acknowledged by StateHealthAdmin · .* · Clock reset scheduled/)).toBeTruthy()
    expect(within(acknowledgedCard).queryByRole('button', { name: /Acknowledge/ })).toBeNull()
    expect(within(resolved).getByText(/Resolved by StateHealthAdmin/)).toBeTruthy()
    expect(within(resolved).getByText(/Reason: Stranded by the 2\.6\.27 recovery image\./)).toBeTruthy()

    fireEvent.click(within(needsAction).getByRole('button', { name: /Resolve/ }))
    let dialog = await screen.findByRole('dialog', { name: 'Resolve alert with reason' })
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Resolve alert with reason' })).toBeNull())
    expect(onClose).not.toHaveBeenCalled()

    fireEvent.click(within(screen.getByRole('region', { name: /Needs action/ })).getByRole('button', { name: /Resolve/ }))
    dialog = await screen.findByRole('dialog', { name: 'Resolve alert with reason' })
    fireEvent.change(within(dialog).getByLabelText('Audited reason'), { target: { value: 'Stranded by the 2.6.27 storage-recovery image.' } })
    fireEvent.change(within(dialog).getByLabelText('Administrator password'), { target: { value: 'local-step-up' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Resolve alert' }))
    await waitFor(() => expect(toast.notice).toHaveBeenCalledWith(expect.stringMatching(/Alert resolved with an audit entry/)))
    const request = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith('/api/v1/alerts/41/resolve'))
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({
      reason: 'Stranded by the 2.6.27 storage-recovery image.',
      password: 'local-step-up',
      idempotency_key: expect.stringMatching(/^alert-resolve:/),
    })
  })
})
