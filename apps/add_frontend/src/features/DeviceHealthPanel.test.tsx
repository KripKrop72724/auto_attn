import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Device, DeviceHealth, HealthReason } from '../types'
import { DeviceHealthPanel, currencyLabel } from './DeviceHealthPanel'

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
  last_error_code: 'ESP_DELIVERY_WORKER_FAULT',
  zkt: null,
}

const reason = (overrides: Partial<HealthReason>): HealthReason => ({
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
  evidence_summary: 'add_delivery STOPPED',
  clear_condition: 'Clears when every required delivery worker reports running with a fresh tick.',
  operator: { resolvable: true, refusal_code: null, refusal: null },
  ...overrides,
})

const health = (overrides: Partial<DeviceHealth> = {}): DeviceHealth => ({
  mode: 'ENFORCED',
  tier: 'ONLINE_WITH_WARNINGS',
  derived_lifecycle: 'ONLINE_WITH_WARNINGS',
  last_error_code: 'ESP_DELIVERY_WORKER_FAULT',
  primary: { code: 'ESP_DELIVERY_WORKER_FAULT', error_code: 'ESP_DELIVERY_WORKER_FAULT', tier: 'WARNING', message: 'Delivery worker add_delivery reported STOPPED.', since: '2026-10-09T08:00:00Z' },
  degraded_count: 0,
  warning_count: 1,
  evaluated_at: '2026-10-10T08:00:00Z',
  lifecycle_state: 'ONLINE_WITH_WARNINGS',
  reasons: [reason({})],
  other_active_alerts: [],
  terminal_link: { state: 'CONNECTED', raw_state: 'ONLINE', since: '2026-10-10T06:00:00Z', reason: 'LINK_UP', message: 'The terminal link is up.' },
  coverage: [
    { key: 'storage', label: 'Storage durability', status: 'NOT_REPORTED_BY_FIRMWARE', detail: 'Not reported by firmware 2.5.2.' },
    { key: 'workers', label: 'Delivery workers', status: 'NOT_REPORTED_BY_FIRMWARE', detail: 'Not reported by firmware 2.5.2.' },
    { key: 'preservation', label: 'Upgrade preservation', status: 'NOT_REPORTED_BY_FIRMWARE', detail: 'Not reported by firmware 2.5.2.' },
    { key: 'led', label: 'LED fault channel', status: 'ACTIVE', detail: 'Local storage or resource failures and fatal boot faults are reported through the LED state.' },
  ],
  device_error: { code: 'ESP_DELIVERY_WORKER_FAULT', message: 'Delivery worker fault.', derived_code: 'ESP_DELIVERY_WORKER_FAULT', backed: true, backing_alert_ids: [41] },
  ...overrides,
})

afterEach(cleanup)

describe('DeviceHealthPanel', () => {
  it('shows what 2.5.2 firmware cannot report and keeps the LED channel visible', () => {
    render(<DeviceHealthPanel device={device} health={health()} />)
    expect(screen.getByRole('heading', { name: 'Why this device is online with warnings' })).toBeTruthy()
    const coverage = screen.getByRole('region', { name: 'What ADD can verify on this firmware' })
    expect(within(coverage).getAllByText(/not reported by firmware 2\.5\.2/i)).toHaveLength(3)
    expect(within(coverage).getByText('Storage durability:')).toBeTruthy()
    expect(within(coverage).getByText(/reported through the LED state/)).toBeTruthy()
  })

  it('composes the coverage text when the backend sends no detail', () => {
    render(<DeviceHealthPanel device={device} health={health({ coverage: [{ key: 'storage', label: 'Storage durability', status: 'NOT_REPORTED_BY_FIRMWARE', detail: null }] })} />)
    expect(screen.getByText('Not reported by firmware 2.5.2.')).toBeTruthy()
  })

  it('explains a latched LED fault and refuses manual resolution', () => {
    const latched = reason({
      code: 'ESP_LOCAL_FAILURE',
      error_code: 'ESP_LOCAL_FAILURE',
      tier: 'DEGRADED',
      currency: 'CURRENT',
      alert_id: 77,
      boot_id: '5e6f7a8b-0000-4000-8000-000000000003',
      firmware_version: '2.6.15',
      binding: null,
      latch: { kind: 'LED_LATCH_NO_IO_ERRORS', source: 'add_connector.c:3764', firmware_version: '2.6.15' },
      message: 'The ESP LED reports LOCAL_FAILURE.',
      clear_condition: 'Clears when the ESP reports a healthy LED state, or at the next reboot.',
      operator: { resolvable: false, refusal_code: 'ALERT_CONDITION_CURRENT', refusal: 'The latest evidence still asserts this condition.' },
    })
    const onResolve = vi.fn()
    render(<DeviceHealthPanel device={{ ...device, state: 'DEGRADED', firmware_version: '2.6.15' }} health={health({ tier: 'DEGRADED', derived_lifecycle: 'DEGRADED', reasons: [latched] })} onResolve={onResolve} onAcknowledge={vi.fn()} />)
    const row = screen.getByRole('listitem', { name: 'ESP local failure' })
    expect(within(row).getByText('Latched until reboot — no I/O errors this boot')).toBeTruthy()
    expect(within(row).getByText('Clears when the ESP reports a healthy LED state, or at the next reboot.')).toBeTruthy()
    const resolve = within(row).getByRole('button', { name: 'Resolve with reason' }) as HTMLButtonElement
    expect(resolve.disabled).toBe(true)
    expect(within(row).getByText('The latest evidence still asserts this condition.')).toBeTruthy()
    fireEvent.click(resolve)
    expect(onResolve).not.toHaveBeenCalled()
  })

  it('maps every currency to operator language', () => {
    const base = reason({})
    expect(currencyLabel({ ...base, currency: 'CURRENT' })).toBe('Current')
    expect(currencyLabel({ ...base, currency: 'HELD' })).toBe('Held: waiting for verified evidence')
    expect(currencyLabel({ ...base, currency: 'PREVIOUS_BOOT' })).toBe('From an earlier boot (firmware 2.6.27, boot 1a2b3c4d…)')
    expect(currencyLabel({ ...base, currency: 'PREVIOUS_BOOT', firmware_version: null, boot_id: null })).toBe('From an earlier boot')
    expect(currencyLabel({ ...base, currency: 'LATCHED' })).toBe('Latched: heartbeats do not re-check it')
    expect(currencyLabel({ ...base, currency: 'CURRENT', latch: { kind: 'LED_LATCH_STORAGE_VERIFIED', source: null, firmware_version: '2.7.0' } }))
      .toBe('Latched until reboot — storage verified this boot')
  })

  it('offers acknowledgement and resolution for eligible alert reasons only', () => {
    const onAcknowledge = vi.fn()
    const onResolve = vi.fn()
    const derived = reason({
      code: 'TERMINAL_DISCONNECTED',
      error_code: null,
      tier: 'WARNING',
      currency: 'CURRENT',
      gating: false,
      severity: null,
      alert_id: null,
      alert_state: null,
      last_seen_at: null,
      boot_id: null,
      firmware_version: null,
      binding: null,
      evidence_summary: null,
      message: 'The terminal has been unreachable for 7 minutes (RETRY_WAIT).',
      clear_condition: 'Clears when the terminal link reconnects.',
      operator: { resolvable: false, refusal_code: 'DERIVED_REASON', refusal: 'Derived from live state; it clears on its own.' },
    })
    render(<DeviceHealthPanel device={device} health={health({ reasons: [reason({}), derived] })} onAcknowledge={onAcknowledge} onResolve={onResolve} />)
    const worker = screen.getByRole('listitem', { name: 'ESP delivery worker fault' })
    fireEvent.click(within(worker).getByRole('button', { name: 'Acknowledge' }))
    expect(onAcknowledge).toHaveBeenCalledWith(41)
    fireEvent.click(within(worker).getByRole('button', { name: 'Resolve with reason' }))
    expect(onResolve).toHaveBeenCalledWith({ id: 41, code: 'ESP_DELIVERY_WORKER_FAULT', message: 'Delivery worker add_delivery reported STOPPED.' })
    expect(within(worker).getByText('Holds HIL and factory work while active.')).toBeTruthy()

    const terminal = screen.getByRole('listitem', { name: 'Terminal disconnected' })
    expect(within(terminal).queryByRole('button')).toBeNull()
    expect(within(terminal).getByText('Live condition')).toBeTruthy()
    expect(within(terminal).queryByText('Holds HIL and factory work while active.')).toBeNull()
  })

  it('shows acknowledgements, other open items and the re-evaluate action for an unbacked error', () => {
    const onClearError = vi.fn()
    const acknowledged = reason({ acknowledged_at: '2026-10-10T07:00:00Z', acknowledged_by: 'StateHealthAdmin' })
    const clockDrift = reason({
      code: 'ZKT_CLOCK_DRIFT',
      error_code: null,
      tier: null,
      currency: 'CURRENT',
      gating: false,
      severity: 'WARNING',
      alert_id: 55,
      message: 'Terminal clock drift exceeds two minutes.',
      clear_condition: 'Clears when the terminal clock is within two minutes of trusted time.',
    })
    render(<DeviceHealthPanel
      device={device}
      health={health({
        reasons: [acknowledged],
        other_active_alerts: [clockDrift],
        device_error: { code: 'ZKT_CONNECTION_FLAPPING', message: null, derived_code: 'ESP_DELIVERY_WORKER_FAULT', backed: false, backing_alert_ids: [] },
      })}
      onAcknowledge={vi.fn()}
      onResolve={vi.fn()}
      onClearError={onClearError}
    />)
    const worker = screen.getByRole('listitem', { name: 'ESP delivery worker fault' })
    expect(within(worker).getByText(/Acknowledged by StateHealthAdmin/)).toBeTruthy()
    expect(within(worker).queryByRole('button', { name: 'Acknowledge' })).toBeNull()
    const others = screen.getByRole('region', { name: 'Other open items (no health effect)' })
    const drift = within(others).getByRole('listitem', { name: 'ZKT clock drift' })
    expect(within(drift).getByText('No health effect')).toBeTruthy()
    expect(screen.getByText(/ADD derives ESP delivery worker fault from the active alerts/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Re-evaluate device error' }))
    expect(onClearError).toHaveBeenCalledTimes(1)
  })

  it('renders the list summary until the detail arrives', () => {
    const summary = health()
    delete summary.reasons
    delete summary.coverage
    render(<DeviceHealthPanel device={device} health={summary} />)
    expect(screen.getByText(/Warning: Delivery worker add_delivery reported STOPPED\./)).toBeTruthy()
  })

  it('keeps older backends readable without a health object', () => {
    const { container, rerender } = render(<DeviceHealthPanel device={{ ...device, state: 'DEGRADED', last_error_code: 'ZKT_SERIAL_MISMATCH' }} />)
    expect(screen.getByRole('heading', { name: 'Why this device is degraded' })).toBeTruthy()
    expect(screen.getByText(/Stored device error: ZKT serial mismatch/)).toBeTruthy()
    expect(screen.queryByText('ACTIVE PROBLEM')).toBeNull()
    rerender(<DeviceHealthPanel device={{ ...device, state: 'ONLINE', last_error_code: null }} />)
    expect(container.textContent).toBe('')
  })
})
