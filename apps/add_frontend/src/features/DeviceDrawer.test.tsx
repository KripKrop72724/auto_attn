import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useToast } from '../App'
import type { CommKeyState, Device } from '../types'
import { DeviceDrawer } from './DeviceDrawer'


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

describe('DeviceDrawer COMM Key controls', () => {
  beforeEach(() => {
    activeDevice = device
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
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

  it('requires the break-glass workflow and hides the revealed key on blur', async () => {
    const toast = { notice: vi.fn(), error: vi.fn() } as unknown as ReturnType<typeof useToast>
    render(
      <DeviceDrawer
        seed={device}
        revision={0}
        onClose={vi.fn()}
        onManageUsers={vi.fn()}
        toast={toast}
      />,
    )

    fireEvent.click(await screen.findByRole('tab', { name: 'control' }))
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
        toast={toast}
      />,
    )

    fireEvent.click(await screen.findByRole('tab', { name: 'control' }))
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
})
