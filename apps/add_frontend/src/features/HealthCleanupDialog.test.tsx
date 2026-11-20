import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { useToast } from '../App'
import type { CleanupPreview, Device } from '../types'
import { expectPlainText } from '../words/testing'
import { HealthCleanupDialog, cleanupConfirmation } from './HealthCleanupDialog'

const P02 = 'bf4badc7-5f9c-42aa-8b3a-8a43f8daeb5e'
const P06 = '233dac02-eb1b-4598-a876-e3a7b1ecfd54'

const device = (connectorId: string, name: string): Device => ({
  connector_id: connectorId,
  hardware_id: connectorId === P02 ? 'e0:72:a1:d7:05:c4' : 'e0:72:a1:d5:08:a0',
  zone_id: `ZONE-${name.toUpperCase().replace(' ', '-')}`,
  zone_name: 'Peshawar',
  device_id: connectorId.slice(0, 4),
  display_name: `ZONE-${name.toUpperCase().replace(' ', '-')}`,
  division: 'health',
  friendly_name: name.replace(/ 0?(\d+)$/, ' · Device $1'),
  state: 'ONLINE_WITH_WARNINGS',
  connected: true,
  firmware_version: '2.5.2',
  onboarding_generation: 3,
  last_onboarded_at: null,
  last_seen_at: '2026-10-10T08:00:00Z',
  current_activity: 'LIVE_CAPTURE',
  last_error_code: 'ESP_DELIVERY_WORKER_FAULT',
  zkt: null,
})
const devices = [device(P02, 'Peshawar 02'), device(P06, 'Peshawar 06'), device('connector-swat-01', 'Swat 01')]

const preview: CleanupPreview = {
  digest: 'd'.repeat(64),
  expires_at: '2099-10-10T08:15:00Z',
  signature: 's'.repeat(64),
  typed_confirmation: 'RESOLVE 2 ALERTS ON 2 DEVICES',
  suggested_first_scope: [
    { connector_id: P02, mac: 'e0:72:a1:d7:05:c4', terminal_serial: 'CJH9211060009' },
    { connector_id: P06, mac: 'e0:72:a1:d5:08:a0', terminal_serial: 'CJH9211060002' },
  ],
  rows: [
    {
      alert_id: 501, connector_id: P02, code: 'ESP_DELIVERY_WORKER_FAULT', state: 'OPEN', class: 'STRANDED_DIAGNOSTICS', default_selected: true,
      rationale: '2.6.27 is a storage-recovery image; it starts no delivery workers by design.',
      evidence: { summary: 'add_delivery STOPPED · ords_delivery STOPPED' },
      raising_firmware: '2.6.27', raising_boot_id: 'aaaaaaaa-0000-4000-8000-000000000001', current_firmware: '2.5.2', current_boot_id: 'bbbbbbbb-0000-4000-8000-000000000002',
      last_seen_at: '2026-10-08T10:00:00Z',
    },
    {
      alert_id: 502, connector_id: P02, code: 'ESP_DURABILITY_FAULT', state: 'OPEN', class: 'STRANDED_DIAGNOSTICS', default_selected: false,
      rationale: 'Storage durability failed on an earlier boot.', evidence: 'durability DEGRADED · write failures 3',
      raising_firmware: '2.6.25', raising_boot_id: 'cccccccc-0000-4000-8000-000000000003', current_firmware: '2.5.2', current_boot_id: 'bbbbbbbb-0000-4000-8000-000000000002',
      last_seen_at: '2026-10-07T10:00:00Z', residual_code: 'ESP_PRESERVATION_UNVERIFIED',
    },
    {
      alert_id: 601, connector_id: P06, code: 'DEVICE_MESSAGE_REJECTED', state: 'OPEN', class: 'CONDITION_CLEARED', default_selected: true,
      rationale: 'Every rejected type is latched and older than 24 hours.', evidence: { types: 'queue_evidence' },
      raising_firmware: '2.6.24', raising_boot_id: 'dddddddd-0000-4000-8000-000000000004', current_firmware: '2.5.2', current_boot_id: 'eeeeeeee-0000-4000-8000-000000000005',
      last_seen_at: '2026-10-06T10:00:00Z',
    },
    {
      alert_id: 602, connector_id: P06, code: 'OTA_DEVICE_ROLLED_BACK', state: 'OPEN', class: 'REVIEW', default_selected: false,
      rationale: 'No later deployment to this device succeeded; latest campaign PAUSED.', evidence: null,
      raising_firmware: '2.6.24', raising_boot_id: null, current_firmware: '2.5.2', current_boot_id: 'eeeeeeee-0000-4000-8000-000000000005',
      last_seen_at: '2026-10-05T10:00:00Z',
    },
  ],
  connectors: [
    {
      connector_id: P02, display_name: 'Peshawar 02', lifecycle_state: 'DEGRADED', derived_lifecycle_before: 'ONLINE_WITH_WARNINGS', derived_lifecycle_after: 'ONLINE',
      last_error_before: 'ESP_DELIVERY_WORKER_FAULT', last_error_after: null, error_fix: false,
      hold_effects: ['HIL CONNECTOR_ERROR_REQUIRES_REVIEW lifted', 'Factory FACTORY_TERMINAL_NOT_READY lifted'], residuals: [],
    },
    {
      connector_id: P06, display_name: 'Peshawar 06', lifecycle_state: 'DEGRADED', derived_lifecycle_before: 'ONLINE_WITH_WARNINGS', derived_lifecycle_after: 'ONLINE_WITH_WARNINGS',
      last_error_before: 'DEVICE_MESSAGE_REJECTED', last_error_after: 'OTA_PREVIOUS_FIRMWARE_OBSERVED', error_fix: true,
      hold_effects: { hil: 'KEPT', factory: 'KEPT' }, residuals: [],
    },
  ],
  blocked: [],
  skipped: [{ alert_id: 603, connector_id: P06, code: 'ESP_OFFLINE', reason: 'device offline' }],
  counts: { rows: 4, selected: 2, blocked: 0, skipped: 1 },
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
let applyResponses: Response[] = []
const toast = () => ({ notice: vi.fn(), error: vi.fn() }) as unknown as ReturnType<typeof useToast>

const requests = (suffix: string) => vi.mocked(fetch).mock.calls
  .filter(([input, init]) => String(input).endsWith(suffix) && init?.method === 'POST')
  .map(([, init]) => JSON.parse(String(init?.body)))

async function openPreview(notices = toast()) {
  render(<HealthCleanupDialog devices={devices} toast={notices} onClose={vi.fn()} onApplied={vi.fn()} initialChosen={[P02, P06]} />)
  const dialog = screen.getByRole('dialog', { name: 'Clean up old problems' })
  expect((within(dialog).getByRole('radio', { name: /Pick devices/ }) as HTMLInputElement).checked).toBe(true)
  expect(within(dialog).getByText('Up to 50 · 2 picked')).toBeTruthy()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Find old problems' }))
  await within(dialog).findByRole('heading', { name: '2. Check the list' })
  return { dialog, notices }
}

const fillApply = (dialog: HTMLElement, typed: string) => {
  fireEvent.change(within(dialog).getByLabelText('What did you do?'), { target: { value: 'Peshawar 02 and 06 recovery cleanup, reviewed with the owner.' } })
  fireEvent.change(within(dialog).getByLabelText(/^Type .* to confirm$/), { target: { value: typed } })
  fireEvent.change(within(dialog).getByLabelText('Your password'), { target: { value: 'local-step-up' } })
}

describe('HealthCleanupDialog', () => {
  beforeEach(() => {
    applyResponses = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.endsWith('/api/v1/device-health/cleanup/preview') && init?.method === 'POST') return json(preview)
      if (path.endsWith('/api/v1/device-health/cleanup/apply') && init?.method === 'POST') {
        return applyResponses.shift() || json({ request_id: 'device-health-cleanup:applied', resolved_alert_ids: [501, 502, 601], residual_alert_ids: [777] })
      }
      throw new Error(`Unexpected request: ${path}`)
    }))
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('previews the picked devices with memory and check-first rows unticked, in plain words', async () => {
    const { dialog } = await openPreview()
    expect(requests('/cleanup/preview')).toEqual([{ connector_ids: [P02, P06] }])
    const worker = within(dialog).getByRole('checkbox', { name: 'Close Link box is stuck sending attendance' }) as HTMLInputElement
    const durability = within(dialog).getByRole('checkbox', { name: 'Close Link box cannot save attendance safely' }) as HTMLInputElement
    const rejection = within(dialog).getByRole('checkbox', { name: 'Close Some information from this device was refused' }) as HTMLInputElement
    const review = within(dialog).getByRole('checkbox', { name: 'Close Software update undone' }) as HTMLInputElement
    expect([worker.checked, durability.checked, rejection.checked, review.checked]).toEqual([true, false, true, false])
    expect(within(dialog).getByText('Leaves a memory reminder open')).toBeTruthy()
    expect(within(dialog).getByText('Check before picking')).toBeTruthy()
    const p02 = within(dialog).getByRole('article', { name: /Peshawar · Device 2/ })
    expect(within(p02).getByText(/HIL CONNECTOR_ERROR_REQUIRES_REVIEW lifted/).closest('[data-technical]')).toBeTruthy()
    expect(within(p02).getByText(/2\.6\.27 · Restart aaaaaaaa/).closest('[data-technical]')).toBeTruthy()
    expect(within(p02).getByText('Saved device problem: Link box is stuck sending attendance → No problem')).toBeTruthy()
    const p06 = within(dialog).getByRole('article', { name: /Peshawar · Device 6/ })
    expect(within(p06).getByText(/hil: KEPT · factory: KEPT/).closest('[data-technical]')).toBeTruthy()
    expect((within(p06).getByRole('checkbox', { name: /^Update Peshawar · Device 6's saved problem/ }) as HTMLInputElement).checked).toBe(true)
    expect(within(dialog).getByText(/The device is offline, so we can't check it\./)).toBeTruthy()
    expect(within(dialog).getByText('RESOLVE 2 ALERTS ON 2 DEVICES').hasAttribute('data-typed-phrase')).toBe(true)
    expectPlainText(dialog, { skip: '[data-typed-phrase]' })
  })

  it('filters the list by kind without changing the ticks', async () => {
    const { dialog } = await openPreview()
    const kinds = within(dialog).getByRole('group', { name: 'Kinds of old problems' })
    fireEvent.click(within(kinds).getByRole('button', { name: /Check before closing/ }))
    expect(within(dialog).queryByRole('checkbox', { name: 'Close Link box is stuck sending attendance' })).toBeNull()
    expect(within(dialog).getByRole('checkbox', { name: 'Close Software update undone' })).toBeTruthy()
    fireEvent.click(within(kinds).getByRole('button', { name: /Check before closing/ }))
    expect((within(dialog).getByRole('checkbox', { name: 'Close Link box is stuck sending attendance' }) as HTMLInputElement).checked).toBe(true)
  })

  it('keeps Close problems disabled until the typed phrase matches and posts the signed selection', async () => {
    const { dialog, notices } = await openPreview()
    const apply = within(dialog).getByRole('button', { name: 'Close problems' }) as HTMLButtonElement
    fillApply(dialog, 'RESOLVE 2 ALERTS')
    expect(apply.disabled).toBe(true)
    fireEvent.change(within(dialog).getByLabelText(/^Type .* to confirm$/), { target: { value: 'RESOLVE 2 ALERTS ON 2 DEVICES' } })
    expect(apply.disabled).toBe(false)

    // Opting in to the memory row changes the phrase and disables the button again.
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Close Link box cannot save attendance safely' }))
    expect(within(dialog).getByLabelText('Type RESOLVE 3 ALERTS ON 2 DEVICES to confirm')).toBeTruthy()
    expect(apply.disabled).toBe(true)
    fireEvent.change(within(dialog).getByLabelText(/^Type .* to confirm$/), { target: { value: cleanupConfirmation(3, 2) } })
    expect(apply.disabled).toBe(false)
    fireEvent.click(apply)

    expect(await within(dialog).findByText(/^Done\. 3 problems closed · 1 reminder left open\./)).toBeTruthy()
    expect(within(dialog).getByText(/device-health-cleanup:applied/).closest('[data-technical]')).toBeTruthy()
    const [body] = requests('/cleanup/apply')
    expect(body).toEqual({
      connector_ids: [P02, P06],
      digest: preview.digest,
      expires_at: preview.expires_at,
      signature: preview.signature,
      alert_ids: [501, 502, 601],
      error_fix_connector_ids: [P06],
      reason: 'Peshawar 02 and 06 recovery cleanup, reviewed with the owner.',
      typed_confirmation: 'RESOLVE 3 ALERTS ON 2 DEVICES',
      password: 'local-step-up',
      idempotency_key: expect.stringMatching(/^device-health-cleanup:/),
    })
    expect(notices.notice).toHaveBeenCalledWith('Old problems closed.')
  })

  it('explains an out-of-date list and a switched-off clean-up without changing anything', async () => {
    applyResponses = [
      json({ detail: { code: 'SCOPE_CHANGED', message: 'Device evidence changed since the preview.' } }, 409),
      json({ detail: { code: 'CLEANUP_DISABLED', message: 'Device health cleanup apply is disabled.' } }, 409),
    ]
    const { dialog } = await openPreview()
    fillApply(dialog, 'RESOLVE 2 ALERTS ON 2 DEVICES')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close problems' }))
    expect(await within(dialog).findByText('This list is out of date. Nothing was changed. Press Look again.')).toBeTruthy()
    expect(within(dialog).getByText('Device evidence changed since the preview.').closest('[data-technical]')).toBeTruthy()
    expect((within(dialog).getByLabelText('Your password') as HTMLInputElement).value).toBe('')
    expect(within(dialog).getAllByRole('button', { name: /Look again/ }).length).toBeGreaterThan(0)

    fireEvent.change(within(dialog).getByLabelText('Your password'), { target: { value: 'local-step-up' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close problems' }))
    expect(await within(dialog).findByText(/Closing old problems is switched off right now\./)).toBeTruthy()
    const [first, second] = requests('/cleanup/apply')
    expect(second.idempotency_key).toBe(first.idempotency_key)
  })

  it('starts with Pick devices and none picked, or checks every device', async () => {
    render(<HealthCleanupDialog devices={devices} toast={toast()} onClose={vi.fn()} />)
    const dialog = screen.getByRole('dialog', { name: 'Clean up old problems' })
    expect((within(dialog).getByRole('radio', { name: /Pick devices/ }) as HTMLInputElement).checked).toBe(true)
    expect(within(dialog).queryByText(/Peshawar 02 \+ 06/)).toBeNull()
    const find = within(dialog).getByRole('button', { name: 'Find old problems' }) as HTMLButtonElement
    expect(find.disabled).toBe(true)
    const picker = within(dialog).getByRole('group', { name: 'Devices to pick from' })
    expect(within(picker).getByText('Health (3)')).toBeTruthy()
    fireEvent.click(within(picker).getByRole('checkbox', { name: /Swat · Device 1/ }))
    fireEvent.click(find)
    await waitFor(() => expect(requests('/cleanup/preview')).toEqual([{ connector_ids: ['connector-swat-01'] }]))
    await within(dialog).findByRole('heading', { name: '2. Check the list' })
    fireEvent.click(within(dialog).getByRole('radio', { name: /All devices/ }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Look again' }))
    await waitFor(() => expect(requests('/cleanup/preview').at(-1)).toEqual({ connector_ids: null }))
  })
})
