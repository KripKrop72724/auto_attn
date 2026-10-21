import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { FirmwareCampaign, FirmwareDeployment, FirmwareRollbackDiagnosis } from '../types'
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
  terminal: { time_sync_after_new_boot_seconds: 22, answered_within_30_seconds: true },
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
  retry: {
    available: true, refusal: null, interrupted_boots: 1, limit: 2,
    stability_warnings: [{
      code: 'ESP_UPTIME_SHORT', connector_id: '06d8706c-8b9e-4a48-b8c7-a76d16da6ed4',
      message: 'Multan 02: the ESP has been up 12 min (the warning threshold is 30 min).',
    }],
  },
  events: [],
}

const campaign = {
  campaign_id: 'campaign-multan-02', zone_id: 'ZONE-MULTAN-02', status: 'PAUSED', deployments: [deployment],
} as unknown as FirmwareCampaign

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
    expect(screen.getByText(/Time sync 22 s after the new boot \(within 30 s\)/)).toBeTruthy()
    expect(screen.getByText(/None of 1 at MULTAN/)).toBeTruthy()
    expect(screen.getByText('Unknown')).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/power suspected/i)
  })

  it('retries the same target only with typed version, step-up and the stability override', async () => {
    const fetchMock = vi.fn(async () => response({
      campaign_id: 'campaign-multan-02', status: 'ACTIVE', deployment_id: 'retry-1',
      retry_of: 'deployment-multan-02', connector_id: deployment.connector_id, stability_warnings_overridden: [],
    }))
    vi.stubGlobal('fetch', fetchMock)
    const notice = vi.fn()
    const onChanged = vi.fn(async () => undefined)
    const onClose = vi.fn()
    render(
      <RetryTargetDialog
        campaign={campaign}
        deployment={deployment}
        onClose={onClose}
        onChanged={onChanged}
        toast={{ notice }}
      />,
    )
    const retry = screen.getByRole('button', { name: 'Retry same target' }) as HTMLButtonElement
    fireEvent.change(screen.getByLabelText('Audited reason'), { target: { value: 'Site replaced the ESP supply.' } })
    fireEvent.change(screen.getByLabelText(/Type exact version/), { target: { value: '2.7.5' } })
    fireEvent.change(screen.getByLabelText('Administrator password'), { target: { value: 'local-step-up-value' } })
    expect(screen.getByText(/up 12 min/)).toBeTruthy()
    expect(retry.disabled).toBe(true)
    fireEvent.click(screen.getByLabelText(/Override the stability warnings/))
    expect(retry.disabled).toBe(false)
    fireEvent.click(retry)
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/v1/firmware/campaigns/campaign-multan-02/retry-target')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({
      deployment_id: 'deployment-multan-02',
      reason: 'Site replaced the ESP supply.',
      typed_confirmation: '2.7.5',
      password: 'local-step-up-value',
      override_stability_warnings: true,
    })
    expect(onChanged).toHaveBeenCalled()
    expect(notice).toHaveBeenCalledWith(expect.stringMatching(/2\.7\.5 attempt for Multan 02 is queued/))
  })

  it('keeps the dialog open with the server refusal', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ detail: 'The ESP is offline.' }, 409)))
    const onClose = vi.fn()
    render(
      <RetryTargetDialog
        campaign={campaign}
        deployment={{ ...deployment, retry: { ...deployment.retry!, stability_warnings: [] } }}
        onClose={onClose}
        onChanged={async () => undefined}
        toast={{ notice: vi.fn() }}
      />,
    )
    fireEvent.change(screen.getByLabelText('Audited reason'), { target: { value: 'Site replaced the ESP supply.' } })
    fireEvent.change(screen.getByLabelText(/Type exact version/), { target: { value: '2.7.5' } })
    fireEvent.change(screen.getByLabelText('Administrator password'), { target: { value: 'local-step-up-value' } })
    fireEvent.click(screen.getByRole('button', { name: 'Retry same target' }))
    expect((await screen.findByRole('alert')).textContent).toContain('The ESP is offline.')
    expect(onClose).not.toHaveBeenCalled()
    expect((screen.getByLabelText('Administrator password') as HTMLInputElement).value).toBe('')
  })
})
