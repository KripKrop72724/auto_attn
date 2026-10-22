import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { useToast } from '../App'
import type { Device, FirmwareRelease } from '../types'
import { HilPlansPanel, type HilPlan } from './HilPlans'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const slow = { timeout: 10_000 }
const toast = () => ({ notice: vi.fn(), error: vi.fn() }) as unknown as ReturnType<typeof useToast>
const target = (connector_id: string, mac: string, terminal_serial: string) => ({ connector_id, mac, terminal_serial })
const T13 = target('4567587c-29ee-4e59-92a4-6c36650a84aa', 'e0:72:a1:d6:3c:7c', 'PGB1261200077')
const P02 = target('bf4badc7-5f9c-42aa-8b3a-8a43f8daeb5e', 'e0:72:a1:d7:05:c4', 'CJH9211060009')
const T3 = target('2ca9a4c2-5ae4-4330-8d14-840223672897', 'a4:cb:8f:d4:66:64', 'PGB1261200074')

const device = (row: typeof T13, zone: string, name: string): Device => ({
  connector_id: row.connector_id, hardware_id: row.mac, zone_id: zone, zone_name: zone, device_id: '1',
  display_name: name, state: 'ONLINE', connected: true, firmware_version: '2.7.3', onboarding_generation: 1,
  last_onboarded_at: null, last_seen_at: '2026-10-22T08:00:00Z', current_activity: null, last_error_code: null, zkt: null,
})
const devices = [device(T13, 'ZONE-SLICTOWER-13FL', 'SLICTOWER 13FL'), device(P02, 'ZONE-PESHAWAR-02', 'Peshawar 02'),
  device(T3, 'ZONE-SLICTOWER-3FL', 'SLICTOWER 3FL')]
const release = {
  release_id: 'zone-lite-2.7.6', version: '2.7.6', firmware_family: 'zkt', git_sha: 'a'.repeat(40),
  image_sha256: 'b'.repeat(64), application_sha256: 'e'.repeat(64), image_size: 1024, state: 'HIL_ONLY',
  partition_layout: 'zone-lite-ota-v1', signing_key_id: 'production-key', published_at: '2026-10-21T08:00:00Z',
  hil_target_mac: null, hil_targets: [T3, P02, T13], hil_allowed_targets: [T3, P02, T13],
} as FirmwareRelease

const plan = (overrides: Partial<HilPlan>): HilPlan => ({
  plan_id: 'a'.repeat(32), status: 'RUNNING',
  release: { release_id: 'zone-lite-2.7.6', version: '2.7.6', image_sha256: 'b'.repeat(64), application_sha256: 'e'.repeat(64) },
  reason: 'Owner approved staged HIL', approved_by: 'StateHealthAdmin', approved_at: '2026-10-22T08:00:00Z',
  expires_at: '2026-10-25T08:00:00Z', stage_timeout_minutes: 360, current_stage: 1, stage_count: 2,
  waiting_reason: null, halt_code: null, halt_reason: null,
  stages: [
    { index: 1, status: 'RUNNING', devices: [
      { zone_id: 'ZONE-SLICTOWER-13FL', connector_id: T13.connector_id, display_name: 'SLICTOWER 13FL', location: 'SLICTOWER', deployment_status: 'DOWNLOADING' },
      { zone_id: 'ZONE-PESHAWAR-02', connector_id: P02.connector_id, display_name: 'Peshawar 02', location: 'PESHAWAR', deployment_status: 'PENDING' },
    ] },
    { index: 2, status: 'QUEUED', devices: [
      { zone_id: 'ZONE-SLICTOWER-3FL', connector_id: T3.connector_id, display_name: 'SLICTOWER 3FL', location: 'SLICTOWER' },
    ] },
  ],
  ...overrides,
})

let plans: HilPlan[] = []
let approvalFailures = 0
const calls = (suffix: string) => vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith(suffix))
const approvals = () => vi.mocked(fetch).mock.calls.filter(([url, init]) => String(url).endsWith('/api/v1/firmware/hil-plans') && init?.method === 'POST')

beforeEach(() => {
  plans = []
  approvalFailures = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/api/v1/firmware/hil-plans') && (!init?.method || init.method === 'GET')) return json({ rows: plans })
    if (url.endsWith('/hil-plans/preview')) {
      return json({
        release: { release_id: 'zone-lite-2.7.6', version: '2.7.6', image_sha256: 'b'.repeat(64), application_sha256: 'e'.repeat(64) },
        stages: [[{ zone_id: 'ZONE-SLICTOWER-13FL', connector_id: T13.connector_id, display_name: 'SLICTOWER 13FL', location: 'SLICTOWER', connected: true, accepted_holds: ['ESP_DURABILITY_FAULT', 'ESP_PRESERVATION_UNVERIFIED'] },
          { zone_id: 'ZONE-PESHAWAR-02', connector_id: P02.connector_id, display_name: 'Peshawar 02', location: 'PESHAWAR', connected: false, accepted_holds: [] }]],
        plan_digest: 'd'.repeat(64), expires_in_hours: 72, stage_timeout_minutes: 360, soak_minutes: 30,
        window_start_hour: 10, window_end_hour: 16,
        slot_holders: [{ connector_id: 'lahore', zone_id: 'ZONE-LAHORE-02', display_name: 'Lahore 02', status: 'RECONCILING', campaign_status: 'CANCELLED', location: 'LAHORE' }],
      })
    }
    if (url.endsWith('/api/v1/firmware/hil-plans')) {
      if (approvalFailures > 0) {
        approvalFailures -= 1
        return json({ detail: 'The connection was reset.' }, 503)
      }
      return json(plan({ stage_count: 1 }), 201)
    }
    if (url.endsWith('/cancel')) return json({ plan_id: 'a'.repeat(32), status: 'CANCELLED' })
    return json({ plan_id: 'a'.repeat(32), status: 'PAUSED' })
  }))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('staged HIL plans', () => {
  it('shows a halted plan with its reason and no controls, and offers controls for an open plan', async () => {
    plans = [
      plan({}),
      plan({ plan_id: 'b'.repeat(32), status: 'HALTED', halt_code: 'DEPLOYMENT_ROLLED_BACK',
        halt_reason: 'ZONE-PESHAWAR-02: deployment ROLLED_BACK (BOOT_HEALTH_TIMEOUT).' }),
    ]
    render(<HilPlansPanel releases={[release]} devices={devices} hilEnabled revision={0} toast={toast()} />)
    const halted = await screen.findByRole('article', { name: `Staged HIL plan ${'b'.repeat(32)}` }, slow)
    expect(within(halted).getByRole('alert').textContent).toContain('DEPLOYMENT_ROLLED_BACK: ZONE-PESHAWAR-02')
    expect(within(halted).queryByRole('button')).toBeNull()
    const running = screen.getByRole('article', { name: `Staged HIL plan ${'a'.repeat(32)}` })
    expect(within(running).getByText(/SLICTOWER 13FL: DOWNLOADING · Peshawar 02: PENDING/)).toBeTruthy()
    expect(within(running).getByRole('button', { name: /Pause/ })).toBeTruthy()
    // One open plan at a time.
    expect((screen.getByRole('button', { name: /New staged plan/ }) as HTMLButtonElement).disabled).toBe(true)
  }, 30_000)

  it('previews the exact plan, then approves it once with the digest and password step-up', async () => {
    const notices = toast()
    render(<HilPlansPanel releases={[release]} devices={devices} hilEnabled revision={0} toast={notices} />)
    await waitFor(() => expect(calls('/api/v1/firmware/hil-plans')).toHaveLength(1), slow)
    fireEvent.click(screen.getByRole('button', { name: /New staged plan/ }))
    fireEvent.change(screen.getByLabelText('HIL-only release'), { target: { value: 'zone-lite-2.7.6' } })
    fireEvent.change(screen.getByLabelText('Stage 1 device 1'), { target: { value: 'ZONE-SLICTOWER-13FL' } })
    // A zone already chosen is not offered again.
    expect(within(screen.getByLabelText('Stage 1 device 2')).queryByText(/ZONE-SLICTOWER-13FL/)).toBeNull()
    fireEvent.change(screen.getByLabelText('Stage 1 device 2'), { target: { value: 'ZONE-PESHAWAR-02' } })
    const approve = screen.getByRole('button', { name: /Approve staged plan/ }) as HTMLButtonElement
    expect(approve.disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: /Preview exact plan/ }))
    expect(await screen.findByText(/Peshawar 02 \(PESHAWAR, offline\)/, undefined, slow)).toBeTruthy()
    expect(JSON.parse(String(calls('/hil-plans/preview')[0][1]?.body))).toEqual({
      release_id: 'zone-lite-2.7.6', stages: [['ZONE-SLICTOWER-13FL', 'ZONE-PESHAWAR-02']],
      expires_in_hours: 72, stage_timeout_minutes: 360, soak_minutes: 30, window_start_hour: 10, window_end_hour: 16,
    })
    // The owner sees exactly which existing conditions the approval accepts, and who holds the slots.
    expect(screen.getByText(/SLICTOWER 13FL: ESP_DURABILITY_FAULT, ESP_PRESERVATION_UNVERIFIED/)).toBeTruthy()
    expect(screen.getByText(/Lahore 02 \(RECONCILING, campaign cancelled\)/)).toBeTruthy()
    expect(screen.getByText(/then 30 minutes on its new boot/).textContent).toContain('10:00–16:00 Pakistan time')
    fireEvent.change(screen.getByLabelText(/Audited reason/), { target: { value: 'Owner approved staged HIL' } })
    fireEvent.change(screen.getByLabelText(/Type exact version/), { target: { value: '2.7.6' } })
    expect(approve.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Administrator password'), { target: { value: 'owner-password' } })
    expect(approve.disabled).toBe(false)
    fireEvent.click(approve)
    await waitFor(() => expect(notices.notice).toHaveBeenCalled(), slow)
    expect(approvals()).toHaveLength(1)
    const body = JSON.parse(String(approvals()[0][1]?.body))
    expect(body).toMatchObject({ release_id: 'zone-lite-2.7.6', stages: [['ZONE-SLICTOWER-13FL', 'ZONE-PESHAWAR-02']],
      plan_digest: 'd'.repeat(64), password: 'owner-password', typed_confirmation: '2.7.6', reason: 'Owner approved staged HIL',
      soak_minutes: 30, window_start_hour: 10, window_end_hour: 16 })
    expect(body.idempotency_key).toMatch(/^hil-plan:/)
  }, 30_000)

  it('retries a failed approval with the same idempotency key', async () => {
    approvalFailures = 1
    const notices = toast()
    render(<HilPlansPanel releases={[release]} devices={devices} hilEnabled revision={0} toast={notices} />)
    await waitFor(() => expect(calls('/api/v1/firmware/hil-plans')).toHaveLength(1), slow)
    fireEvent.click(screen.getByRole('button', { name: /New staged plan/ }))
    fireEvent.change(screen.getByLabelText('HIL-only release'), { target: { value: 'zone-lite-2.7.6' } })
    fireEvent.change(screen.getByLabelText('Stage 1 device 1'), { target: { value: 'ZONE-SLICTOWER-13FL' } })
    fireEvent.click(screen.getByRole('button', { name: /Preview exact plan/ }))
    expect(await screen.findByText(/Pinned plan/, undefined, slow)).toBeTruthy()
    const fill = () => {
      fireEvent.change(screen.getByLabelText(/Audited reason/), { target: { value: 'Owner approved staged HIL' } })
      fireEvent.change(screen.getByLabelText(/Type exact version/), { target: { value: '2.7.6' } })
      fireEvent.change(screen.getByLabelText('Administrator password'), { target: { value: 'owner-password' } })
      fireEvent.click(screen.getByRole('button', { name: /Approve staged plan/ }))
    }
    fill()
    expect((await screen.findByRole('alert', undefined, slow)).textContent).toContain('connection was reset')
    fill()
    await waitFor(() => expect(notices.notice).toHaveBeenCalled(), slow)
    const keys = approvals().map(([, init]) => JSON.parse(String(init?.body)).idempotency_key)
    expect(keys).toHaveLength(2)
    expect(keys[0]).toBe(keys[1])
  }, 30_000)

  it('a changed stage invalidates the preview', async () => {
    render(<HilPlansPanel releases={[release]} devices={devices} hilEnabled revision={0} toast={toast()} />)
    await waitFor(() => expect(calls('/api/v1/firmware/hil-plans')).toHaveLength(1), slow)
    fireEvent.click(screen.getByRole('button', { name: /New staged plan/ }))
    fireEvent.change(screen.getByLabelText('HIL-only release'), { target: { value: 'zone-lite-2.7.6' } })
    fireEvent.change(screen.getByLabelText('Stage 1 device 1'), { target: { value: 'ZONE-SLICTOWER-13FL' } })
    fireEvent.click(screen.getByRole('button', { name: /Preview exact plan/ }))
    expect(await screen.findByText(/Pinned plan/, undefined, slow)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Add stage/ }))
    expect(screen.queryByText(/Pinned plan/)).toBeNull()
  }, 30_000)

  it('cancels at once and says an installation in progress continues under its campaign', async () => {
    plans = [plan({})]
    const notices = toast()
    render(<HilPlansPanel releases={[release]} devices={devices} hilEnabled revision={0} toast={notices} />)
    fireEvent.click(await screen.findByRole('button', { name: /Cancel plan/ }, slow))
    expect(screen.getByText(/A device already receiving bytes is never interrupted/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText(/Audited reason/), { target: { value: 'Owner cancels this plan' } })
    fireEvent.change(screen.getByLabelText('Administrator password'), { target: { value: 'owner-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm cancel' }))
    await waitFor(() => expect(notices.notice).toHaveBeenCalledWith('Staged HIL plan cancelled with an audit entry.'), slow)
    expect(JSON.parse(String(calls('/cancel')[0][1]?.body))).toEqual({ reason: 'Owner cancels this plan', password: 'owner-password' })
  }, 30_000)

  it('pause says unoffered stage devices are held too', async () => {
    plans = [plan({ soak_minutes: 30, window_start_hour: 10, window_end_hour: 16 })]
    render(<HilPlansPanel releases={[release]} devices={devices} hilEnabled revision={0} toast={toast()} />)
    expect(await screen.findByText(/Soak 30 min · stage timeout 360 min · stages start 10:00–16:00 Pakistan time/, undefined, slow)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Pause/ }))
    expect(screen.getByText(/Stage devices that were never offered bytes are held too/)).toBeTruthy()
  }, 30_000)
})
