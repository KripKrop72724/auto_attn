import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type {
  FirmwareCampaign,
  FirmwareDeployment,
  FirmwareRetryEspState,
  FirmwareRetryPreflight,
  FirmwareRollbackDiagnosis,
} from '../types'
import { RetryTargetDialog, RollbackEvidence } from './FirmwareRetry'

const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const diagnosis: FirmwareRollbackDiagnosis = {
  classification: 'TARGET_BOOT_INTERRUPTED',
  unclassified_reasons: [],
  target_version: '2.7.5',
  previous_version: '2.6.15',
  target_boot: {
    estimated_lifetime_seconds: { min: 20, max: 53 },
    last_progress_at: '2026-10-21T08:01:39Z',
    last_progress_state: 'BOOTED_PENDING',
    last_progress_code: 'WAITING_FOR_RUNTIME_HEALTH',
    progress_reports: 1,
    failure_reports: [],
    logs_received: 0,
    telemetry_received: 0,
  },
  previous_firmware_resets: { resets: 46, boot_ids: 47, online_hours: 30, reset_rate_per_online_hour: 1.533, days: 7 },
  terminal: { time_sync_after_new_boot_seconds: 22, answered_within_30_seconds: true, bound: 'upper', source: 'received_at' },
  location_resets: {
    location: 'MULTAN', peers_checked: 1, window_seconds: 120, complete: true,
    resets: [],
  },
  reconcile_at_reset: { running: null, last_target_activity: null },
}

const deployment: FirmwareDeployment = {
  deployment_id: 'deployment-multan-02',
  connector_id: '06d8706c-8b9e-4a48-b8c7-a76d16da6ed4',
  display_name: 'Multan 02',
  hardware_id: 'ac:27:6e:a3:de:e8',
  zone_id: 'ZONE-MULTAN-02',
  status: 'ROLLED_BACK',
  previous_version: '2.6.15',
  target_version: '2.7.5',
  bytes_written: 1_572_864,
  attempt_count: 1,
  error_code: 'TARGET_BOOT_INTERRUPTED',
  error_message: 'The ESP reset before the image was confirmed.',
  offered_at: '2026-10-21T07:59:00Z',
  completed_at: '2026-10-21T08:02:41Z',
  updated_at: '2026-10-21T08:02:41Z',
  transport_diagnostics: null,
  rollback_diagnosis: diagnosis,
  retry: { available: true, refusal: null, interrupted_boots: 1, limit: 2, pending_deployments: 0 },
  events: [],
}

const espState: FirmwareRetryEspState = {
  connected: true, firmware_version: 'zone-lite-2.6.15', boot_id: 'boot-ret', storage_reported: true,
  storage_durability: 'DEGRADED', storage_io_faults: null, diagnostics_at: '2026-10-21T08:30:00Z',
  outbox_depth: 0, last_heartbeat_at: '2026-10-21T08:30:00Z',
}

const preflight = (changes: Partial<FirmwareRetryPreflight> = {}): FirmwareRetryPreflight => ({
  campaign_id: 'campaign-multan-02', deployment_id: 'deployment-multan-02', target_version: '2.7.5',
  available: true, refusal: null, interrupted_boots: 1, limit: 2, pending_deployments: 0,
  stability_warnings: [{
    code: 'ESP_UPTIME_SHORT', connector_id: '06d8706c-8b9e-4a48-b8c7-a76d16da6ed4',
    message: 'Multan 02: the ESP has been up 12 min (the warning threshold is 30 min).',
  }],
  stability_warnings_digest: 'a'.repeat(64),
  esp_state: espState,
  ...changes,
})

const retried = {
  campaign_id: 'campaign-multan-02', status: 'ACTIVE', deployment_id: 'retry-1',
  retry_of: 'deployment-multan-02', connector_id: deployment.connector_id, stability_warnings_overridden: [],
}

// The dialog reads the preflight (GET) when it opens, then posts the retry.
const server = (review: FirmwareRetryPreflight, posted: () => Response = () => response(retried)) =>
  vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => (init?.method === 'POST' ? posted() : response(review)))

const posts = (fetchMock: ReturnType<typeof server>) =>
  fetchMock.mock.calls.filter((call) => (call as unknown as [string, RequestInit?])[1]?.method === 'POST') as unknown as Array<
    [string, RequestInit]
  >

const campaign = {
  campaign_id: 'campaign-multan-02', zone_id: 'ZONE-MULTAN-02', status: 'PAUSED', deployments: [deployment],
} as unknown as FirmwareCampaign

const dialog = (onClose = vi.fn(), notice = vi.fn(), onChanged = vi.fn(async () => undefined)) =>
  render(
    <RetryTargetDialog
      campaign={campaign}
      deployment={deployment}
      onClose={onClose}
      onChanged={onChanged}
      toast={{ notice }}
    />,
  )

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('interrupted target boot', () => {
  it('shows the rollback evidence without naming a cause', () => {
    render(<RollbackEvidence diagnosis={diagnosis} />)
    expect(screen.getByRole('heading', { name: 'Target boot interrupted' })).toBeTruthy()
    expect(screen.getByText('20 s to 53 s')).toBeTruthy()
    expect(screen.getByText(/46 in 30 online hours over 7 days/)).toBeTruthy()
    expect(screen.getByText(/Time sync received 22 s after the new boot \(within 30 s\)/)).toBeTruthy()
    expect(screen.getByText(/None of 1 at MULTAN/)).toBeTruthy()
    expect(screen.getByText('Unknown')).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/power suspected/i)
  })

  it('retries the same target with one click, overriding exactly the reviewed stability warnings', async () => {
    const fetchMock = server(preflight())
    vi.stubGlobal('fetch', fetchMock)
    const notice = vi.fn()
    const onChanged = vi.fn(async () => undefined)
    const onClose = vi.fn()
    dialog(onClose, notice, onChanged)
    expect(await screen.findByText(/up 12 min/)).toBeTruthy()
    const [review] = fetchMock.mock.calls[0] as unknown as [string, RequestInit?]
    expect(review).toBe('/api/v1/firmware/campaigns/campaign-multan-02/retry-target?deployment_id=deployment-multan-02')
    expect(screen.getByText(/DEGRADED · no I\/O faults reported/)).toBeTruthy()
    expect(screen.getByText(/^0 queued at/)).toBeTruthy()
    expect(screen.queryByLabelText('Administrator password')).toBeNull()
    expect(screen.queryByLabelText('Audited reason')).toBeNull()
    expect(screen.queryByLabelText(/Type exact version/)).toBeNull()
    expect(screen.queryByLabelText(/Override the stability warnings/)).toBeNull()
    const retry = screen.getByRole('button', { name: 'Retry same target' }) as HTMLButtonElement
    expect(retry.disabled).toBe(false)
    fireEvent.click(retry)
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    const [[path, init]] = posts(fetchMock)
    expect(path).toBe('/api/v1/firmware/campaigns/campaign-multan-02/retry-target')
    expect(JSON.parse(String(init.body))).toEqual({
      deployment_id: 'deployment-multan-02',
      override_stability_warnings: true,
      stability_warnings_digest: 'a'.repeat(64),
      resume_pending_deployments: 0,
    })
    expect(onChanged).toHaveBeenCalled()
    expect(notice).toHaveBeenCalledWith(expect.stringMatching(/2\.7\.5 attempt for Multan 02 is queued/))
  })

  it('sends the pending deployments the retry also resumes and says so', async () => {
    const fetchMock = server(preflight({ stability_warnings: [], pending_deployments: 2 }))
    vi.stubGlobal('fetch', fetchMock)
    const onClose = vi.fn()
    const notice = vi.fn()
    dialog(onClose, notice)
    expect(await screen.findByText(/also offers its 2 other pending deployments/)).toBeTruthy()
    const retry = screen.getByRole('button', { name: 'Retry same target' }) as HTMLButtonElement
    expect(retry.disabled).toBe(false)
    fireEvent.click(retry)
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    const body = JSON.parse(String(posts(fetchMock)[0][1].body))
    expect(body.resume_pending_deployments).toBe(2)
    expect(body.override_stability_warnings).toBe(false)
    expect(body).not.toHaveProperty('stability_warnings_digest')
    expect(notice).toHaveBeenCalledWith(expect.stringMatching(/also offers its 2 other pending deployments/))
  })

  it('shows the server refusal from the review and never offers the retry', async () => {
    const fetchMock = server(preflight({
      available: false, stability_warnings: [], esp_state: { ...espState, storage_io_faults: { write_failures: 3 } },
      refusal: 'The ESP reports storage I/O faults on its current boot (write_failures=3); recover its storage before another attempt.',
    }))
    vi.stubGlobal('fetch', fetchMock)
    dialog()
    expect((await screen.findByRole('alert')).textContent).toContain('storage I/O faults')
    expect(screen.getByText('I/O faults: write_failures=3')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Retry same target' }) as HTMLButtonElement).disabled).toBe(true)
    expect(posts(fetchMock)).toHaveLength(0)
  })

  it('shows an inconsistent time sync without a figure', () => {
    render(<RollbackEvidence diagnosis={{
      ...diagnosis,
      terminal: { time_sync_after_new_boot_seconds: null, answered_within_30_seconds: null, source: 'inconsistent' },
    }} />)
    expect(screen.getByText('Time sync timing inconsistent with the boot estimate')).toBeTruthy()
  })

  it('keeps the dialog open with the server refusal', async () => {
    vi.stubGlobal('fetch', server(preflight({ stability_warnings: [] }),
      () => response({ detail: 'The ESP is offline.' }, 409)))
    const onClose = vi.fn()
    dialog(onClose)
    await screen.findByText(/Current ESP state/)
    fireEvent.click(screen.getByRole('button', { name: 'Retry same target' }))
    expect((await screen.findByRole('alert')).textContent).toContain('The ESP is offline.')
    expect(onClose).not.toHaveBeenCalled()
  })
})

describe('downloaded image that did not verify (ZKT 2.7.7)', () => {
  it('explains a fresh download instead of an interrupted boot', async () => {
    vi.stubGlobal('fetch', server(preflight({ kind: 'PARTITION_HASH_FAILED', stability_warnings: [] })))
    render(
      <RetryTargetDialog
        campaign={campaign}
        deployment={{
          ...deployment,
          status: 'FAILED',
          error_code: 'PARTITION_HASH_FAILED',
          rollback_diagnosis: null,
          retry: { ...deployment.retry!, kind: 'PARTITION_HASH_FAILED' },
        }}
        onClose={vi.fn()}
        onChanged={vi.fn(async () => undefined)}
        toast={{ notice: vi.fn() }}
      />,
    )
    expect(screen.getByText(/did not verify on the ESP/)).toBeTruthy()
    expect(screen.getByText(/downloads it again from\s+the start/)).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/reset before it confirmed/)
    await waitFor(() => expect(screen.queryByText(/Checking the ESP/)).toBeNull())
  })
})
