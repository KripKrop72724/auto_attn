import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { Device, DeviceHealth, HealthReason } from '../types'
import { HealthEvidence, currencyLabel } from './DeviceHealthPanel'
import { problemWords } from '../words/problems'

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

// Health evidence lives in Summary › Technical details (04-device §2); the
// headline and the Mark as seen / fixed buttons moved to the card and the
// Problems tab, so this checks the evidence parts only.
describe('HealthEvidence', () => {
  it('shows what 2.5.2 software cannot report and keeps the LED channel visible', () => {
    render(<HealthEvidence device={device} health={health()} />)
    const coverage = screen.getByRole('heading', { name: 'What this link box software can report' }).closest('section') as HTMLElement
    expect(within(coverage).getAllByText(/not reported by firmware 2\.5\.2/i)).toHaveLength(3)
    expect(within(coverage).getByText('Storage durability:')).toBeTruthy()
    expect(within(coverage).getByText(/reported through the LED state/)).toBeTruthy()
  })

  it('composes the coverage text when the backend sends no detail', () => {
    render(<HealthEvidence device={device} health={health({ coverage: [{ key: 'storage', label: 'Storage durability', status: 'NOT_REPORTED_BY_FIRMWARE', detail: null }] })} />)
    expect(screen.getByText('Not reported by link box software 2.5.2.')).toBeTruthy()
  })

  it('explains a latched fault, its clear condition and why it cannot be marked as fixed, with no buttons', () => {
    const latched = reason({
      code: 'ESP_LOCAL_FAILURE',
      error_code: 'ESP_LOCAL_FAILURE',
      tier: 'DEGRADED',
      currency: 'CURRENT',
      alert_id: 77,
      latch: { kind: 'LED_LATCH_NO_IO_ERRORS', source: 'add_connector.c:3764', firmware_version: '2.6.15' },
      message: 'The ESP LED reports LOCAL_FAILURE.',
      clear_condition: 'Clears when the ESP reports a healthy LED state, or at the next reboot.',
      operator: { resolvable: false, refusal_code: 'ALERT_CONDITION_CURRENT', refusal: 'The latest evidence still asserts this condition.' },
    })
    render(<HealthEvidence device={{ ...device, state: 'DEGRADED' }} health={health({ tier: 'DEGRADED', reasons: [latched] })} />)
    const row = screen.getByRole('listitem', { name: problemWords({ code: 'ESP_LOCAL_FAILURE' }).title })
    expect(within(row).getByText('Kept until the next restart: no memory errors since this restart')).toBeTruthy()
    expect(within(row).getByText('Clears when the ESP reports a healthy LED state, or at the next reboot.')).toBeTruthy()
    expect(within(row).getByText('The latest evidence still asserts this condition.')).toBeTruthy()
    expect(within(row).getByText('Needs attention')).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('maps every currency to plain words', () => {
    const base = reason({})
    expect(currencyLabel({ ...base, currency: 'CURRENT' })).toBe('Reported now')
    expect(currencyLabel({ ...base, currency: 'HELD' })).toBe('Waiting for the link box to confirm it')
    expect(currencyLabel({ ...base, currency: 'PREVIOUS_BOOT' })).toBe('From before the last restart (software 2.6.27, restart 1a2b3c4d…)')
    expect(currencyLabel({ ...base, currency: 'PREVIOUS_BOOT', firmware_version: null, boot_id: null })).toBe('From before the last restart')
    expect(currencyLabel({ ...base, currency: 'LATCHED' })).toBe('Kept until it is checked again')
    expect(currencyLabel({ ...base, currency: 'CURRENT', latch: { kind: 'LED_LATCH_STORAGE_VERIFIED', source: null, firmware_version: '2.7.0' } }))
      .toBe('Kept until the next restart: memory checked since this restart')
  })

  it('says a gating reason pauses test-device work and a derived one is happening now', () => {
    const derived = reason({ code: 'ZKT_LINK_DOWN', error_code: null, alert_id: null, alert_state: null, last_seen_at: null, gating: false, tier: 'DEGRADED' })
    render(<HealthEvidence device={device} health={health({ reasons: [reason({}), derived] })} />)
    const worker = screen.getByRole('listitem', { name: problemWords({ code: 'ESP_DELIVERY_WORKER_FAULT' }).title })
    expect(within(worker).getByText('While open, test-device updates wait.')).toBeTruthy()
    expect(within(worker).getByText('#41')).toBeTruthy()
    const link = screen.getByRole('listitem', { name: problemWords({ code: 'ZKT_LINK_DOWN' }).title })
    expect(within(link).getByText('Happening now')).toBeTruthy()
    expect(within(link).queryByText('While open, test-device updates wait.')).toBeNull()
  })

  it('says whether an open problem keeps the saved error, and shows other open items', () => {
    const drift = reason({ code: 'ZKT_CLOCK_DRIFT', error_code: 'ZKT_CLOCK_DRIFT', tier: null, alert_id: 43, gating: false })
    const { rerender } = render(<HealthEvidence device={device} health={health({ other_active_alerts: [drift] })} />)
    expect(screen.getByText('Kept by open problem #41. It clears with that problem.')).toBeTruthy()
    const others = screen.getByRole('heading', { name: 'Also open (does not change the status)' }).closest('section') as HTMLElement
    expect(within(others).getByText('Does not change the status')).toBeTruthy()
    rerender(<HealthEvidence device={device} health={health({ device_error: { code: 'ZKT_CONNECTION_FLAPPING', message: null, derived_code: null, backed: false, backing_alert_ids: [] } })} />)
    expect(screen.getByText('No open problem keeps this saved error. It is out of date.')).toBeTruthy()
  })

  it('labels the shadow model as a preview and shows the machine connection', () => {
    render(<HealthEvidence device={device} health={health({ mode: 'SHADOW' })} />)
    expect(screen.getByRole('heading', { name: /^Preview of the new status rules: / })).toBeTruthy()
    expect(screen.getByText('Preview only. The status at the top still decides the totals and problems.')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Machine connection' })).toBeTruthy()
  })

  it('keeps older backends readable without a health object', () => {
    const { container, rerender } = render(<HealthEvidence device={{ ...device, last_error_code: 'ZKT_SERIAL_MISMATCH' }} health={null} />)
    expect(screen.getByText(/Saved error: .*\(ZKT_SERIAL_MISMATCH\)/)).toBeTruthy()
    rerender(<HealthEvidence device={{ ...device, last_error_code: null }} health={null} />)
    expect(container.textContent).toBe('No health details reported.')
  })
})
