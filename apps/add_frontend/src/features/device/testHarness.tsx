// Test-only helpers for the device view tests (never imported by app code).
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { vi } from 'vitest'
import { queryClient } from '../../data'
import { deviceSnapshots } from '../../deviceData'
import type { Alert, CommKeyState, Device, DeviceHealth, HealthReason } from '../../types'
import { ToastProvider } from '../../ui'
import { DeviceDrawer } from '../DeviceDrawer'

export const device: Device = {
  connector_id: 'connector-quetta',
  hardware_id: '00:17:61:12:03:32',
  zone_id: 'QUETTA',
  zone_name: 'Quetta',
  device_id: '1',
  display_name: 'QUETTA-01',
  friendly_name: 'Quetta',
  division: 'health',
  state: 'ONLINE',
  connected: true,
  firmware_version: 'zone-lite-2.5.0',
  comm_key_capable: true,
  comm_key_revision: 1,
  onboarding_generation: 1,
  last_onboarded_at: null,
  last_seen_at: new Date().toISOString(),
  current_activity: 'ONLINE',
  last_error_code: null,
  zkt: null,
}

export const zkt = (overrides: Partial<NonNullable<Device['zkt']>> = {}): NonNullable<Device['zkt']> => ({
  id: 7,
  serial: 'AEH2232460004',
  expected_serial: 'AEH2232460004',
  confirmed_serial: 'AEH2232460004',
  ip_address: '192.168.1.250',
  model: 'uFace800',
  platform: 'ZMM720_TFT',
  online: true,
  connection_state: 'ONLINE',
  consecutive_failures: 0,
  consecutive_successes: 1,
  last_transition_at: null,
  last_online_at: null,
  offline_since: null,
  stability_since: null,
  backoff_until: null,
  probe_latency_ms: 5,
  certification_state: 'CERTIFIED',
  certification_observations: 2,
  capabilities: { protocol_restart: true },
  snapshot_complete: true,
  identity_snapshot_observed_at: new Date(Date.now() - 600_000).toISOString(),
  identity_snapshot_stable: true,
  writes_disabled_reason: null,
  user_count: 189,
  attendance_count: 5000,
  device_time: new Date().toISOString(),
  device_time_sampled_at: new Date().toISOString(),
  drift_seconds: 4,
  last_reconcile_at: null,
  next_restart_at: null,
  ...overrides,
} as NonNullable<Device['zkt']>)

export const commKeyState: CommKeyState = {
  enabled: true,
  reveal_enabled: true,
  management_state: 'APPLIED',
  applied_revision: 1,
  desired_revision: 1,
  last_verified_at: new Date().toISOString(),
  verified_terminal_serial: 'UFS2253100068',
  last_error_code: null,
  managed: true,
  capabilities: { esp_only: true, esp_and_terminal: false, esp_and_terminal_block_reason: 'TERMINAL_MODEL_NOT_CERTIFIED', recovery_staging: false },
  active_operation: null,
}

export const workerReason: HealthReason = {
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

export const durabilityReason: HealthReason = {
  ...workerReason,
  code: 'ESP_DURABILITY_FAULT',
  error_code: 'ESP_DURABILITY_FAULT',
  tier: 'DEGRADED',
  currency: 'HELD',
  message: 'Storage durability is DEGRADED.',
  since: '2026-10-10T07:00:00Z',
  alert_id: 42,
  operator: { resolvable: false, refusal_code: 'ALERT_CONDITION_CURRENT', refusal: 'Held on this boot until the device reports verified evidence.' },
}

export const fullHealth = (overrides: Partial<DeviceHealth> = {}): DeviceHealth => ({
  mode: 'ENFORCED',
  tier: 'DEGRADED',
  derived_lifecycle: 'DEGRADED',
  last_error_code: 'ESP_DURABILITY_FAULT',
  primary: { code: 'ESP_DURABILITY_FAULT', error_code: 'ESP_DURABILITY_FAULT', tier: 'DEGRADED', message: durabilityReason.message, since: durabilityReason.since },
  degraded_count: 1,
  warning_count: 1,
  evaluated_at: new Date().toISOString(),
  lifecycle_state: 'DEGRADED',
  reasons: [durabilityReason, workerReason],
  other_active_alerts: [],
  terminal_link: { state: 'CONNECTED', raw_state: 'ONLINE', since: '2026-10-10T06:00:00Z', reason: 'LINK_UP', message: 'The terminal link is up.' },
  coverage: [{ key: 'storage', label: 'Storage durability', status: 'REPORTED', detail: 'DEGRADED; persistence verified; recovery complete.' }],
  device_error: { code: 'ESP_DURABILITY_FAULT', message: 'Storage durability is DEGRADED.', derived_code: 'ESP_DURABILITY_FAULT', backed: true, backing_alert_ids: [42] },
  ...overrides,
})

export const alertRow = (overrides: Partial<Alert>): Alert => ({
  id: 41,
  code: 'ESP_DELIVERY_WORKER_FAULT',
  severity: 'HIGH',
  state: 'OPEN',
  message: 'Delivery worker add_delivery reported STOPPED.',
  details: {},
  first_seen_at: '2026-10-09T08:00:00Z',
  last_seen_at: '2026-10-09T09:00:00Z',
  acknowledged_at: null,
  acknowledged_by: null,
  resolved_at: null,
  resolution: null,
  ...overrides,
})

export const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

export type Handler = (path: string, init?: RequestInit) => Response | Promise<Response> | undefined

/** Stubs fetch: the first handler that answers wins; unknown requests fail the test. */
export function stubFetch(...handlers: Handler[]) {
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    for (const handler of handlers) {
      const answer = await handler(path, init)
      if (answer) return answer
    }
    throw new Error(`Unexpected request: ${path}`)
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

export const calls = (pattern: RegExp | string) => vi.mocked(fetch).mock.calls
  .filter(([input]) => (typeof pattern === 'string' ? String(input).includes(pattern) : pattern.test(String(input))))

export const bodyOf = (pattern: RegExp | string) => {
  const found = calls(pattern).at(-1)
  return found?.[1]?.body ? JSON.parse(String(found[1].body)) : undefined
}

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location" hidden>{`${location.pathname}${location.search}`}</output>
}

export const currentUrl = () => screen.getByTestId('location').textContent || ''

export function resetDeviceTests() {
  queryClient.clear()
  deviceSnapshots.clear()
}

/** Renders the device view at /devices/:id (with an optional ?query) inside the app's providers. */
export function renderDevice(seed: Device = device, { search = '', revision = 0, onInventoryChanged = vi.fn(async () => undefined), onManageUsers = vi.fn() } = {}) {
  const toast = { toast: null, notice: vi.fn(), error: vi.fn(), dismiss: vi.fn() }
  const onClose = vi.fn()
  const view = (rev: number) => (
    <MemoryRouter initialEntries={[`/devices/${seed.connector_id}${search}`]}>
      <ToastProvider>
        <Routes>
          <Route path="*" element={<><DeviceDrawer seed={seed} revision={rev} onClose={onClose} onManageUsers={onManageUsers} onInventoryChanged={onInventoryChanged} toast={toast} /><LocationProbe /></>} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>
  )
  const result = render(view(revision))
  return { ...result, toast, onClose, onInventoryChanged, onManageUsers, rerenderRevision: (next: number) => result.rerender(view(next)) }
}
