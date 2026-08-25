import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

describe('DeviceDrawer COMM Key controls', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
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
      if (path.endsWith(`/devices/${device.connector_id}`)) return response(device)
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
})
