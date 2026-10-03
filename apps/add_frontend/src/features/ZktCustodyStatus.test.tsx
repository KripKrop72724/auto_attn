import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ZktCustodyStatus, type CustodySnapshot } from './ZktCustodyStatus'

const fixture = (updates: Partial<CustodySnapshot> = {}): CustodySnapshot => ({
  connector_id: 'fixture-connector', enabled: true, sampled_at: new Date().toISOString(),
  oracle_completion: 'NOT_ASSERTED', missing_processing_obligation: false,
  counts: [{ state: 'WAIT_PROFILE', owner: 'ADD_PROTOCOL', count: 12 }],
  rows: [{ id: 1, state: 'WAIT_PROFILE', reason_code: 'PROFILE_QUALIFICATION_REQUIRED',
    owner: 'ADD_PROTOCOL', updated_at: new Date().toISOString(), next_attempt_at: null }],
  next_cursor: null, ...updates,
})
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { 'Content-Type': 'application/json' },
})
const mount = () => render(<ZktCustodyStatus connectorId="fixture-connector" revision={0} />)
const processor = (updates: Partial<NonNullable<CustodySnapshot['processor']>> = {}): NonNullable<CustodySnapshot['processor']> => ({
  schema_version: 1, instance_id: '11111111-2222-4333-8444-555555555555', sampled_at: new Date().toISOString(),
  state: 'IDLE', last_completed_at: new Date().toISOString(), last_progress_at: null,
  inspected_groups_total: 0, successful_ticks: 3, failed_ticks: 1, last_tick_ms: 8, ...updates,
})
beforeEach(() => { vi.stubGlobal('fetch', vi.fn(async () => response(fixture()))) })
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('ZKT custody evidence', () => {
  it.each(['IDLE', 'STALLED', 'STOPPED', 'RETRYING'] as const)('reports global worker %s independently of receipt and Oracle counts', async state => {
    vi.mocked(fetch).mockResolvedValue(response(fixture({ processor: processor({ state }) })))
    mount()
    expect(await screen.findByRole('heading', { name: 'ADD worker · all ZKT connectors' })).toBeTruthy()
    expect(screen.getByText(state.charAt(0) + state.slice(1).toLowerCase())).toBeTruthy()
    expect(screen.getByText('3 / 1')).toBeTruthy()
    expect(screen.getByText('8 ms')).toBeTruthy()
    expect(screen.getByText('12')).toBeTruthy()
    expect(screen.getByText('Not established by custody')).toBeTruthy()
    expect(screen.queryByText(/Healthy|All delivered/i)).toBeNull()
  })
  it('marks a stale worker sample historical even if the custody query is fresh', async () => {
    const old = new Date(Date.now() - 60_000).toISOString()
    vi.mocked(fetch).mockResolvedValue(response(fixture({ processor: processor({ sampled_at: old, last_completed_at: old }) })))
    mount()
    expect(await screen.findByText('Last reported worker state')).toBeTruthy()
    expect(screen.queryByText('Worker state')).toBeNull()
  })
  it('keeps invalid future worker evidence unavailable without erasing custody counts', async () => {
    vi.mocked(fetch).mockResolvedValue(response(fixture({ processor: processor({ last_completed_at: new Date(Date.now() + 60_000).toISOString() }) })))
    mount()
    expect(await screen.findByText('Worker progress evidence is unavailable.')).toBeTruthy()
    expect(screen.getByText('12')).toBeTruthy()
  })
  it('shows preserved holds, responsibility and independent Oracle status', async () => {
    mount()
    expect(await screen.findByRole('heading', { name: 'ADD custody processing' })).toBeTruthy()
    expect(screen.getByText('Waiting for profile qualification · Protocol review')).toBeTruthy()
    expect(screen.getByText('12')).toBeTruthy()
    expect(screen.getByText('Not established by custody')).toBeTruthy()
    expect(screen.getByText(/counts are not punch totals/)).toBeTruthy()
    expect(screen.queryByText(/Oracle complete|Healthy|All delivered/i)).toBeNull()
  })
  it('keeps a disabled writer distinct from successful delivery', async () => {
    vi.mocked(fetch).mockResolvedValue(response(fixture({ enabled: false, counts: [], rows: [] })))
    mount()
    expect(await screen.findByRole('heading', { name: 'Journal custody is not enabled' })).toBeTruthy()
    expect(screen.getByText('No journal processing groups recorded.')).toBeTruthy()
  })
  it('makes missing processing work actionable without calling records lost', async () => {
    vi.mocked(fetch).mockResolvedValue(response(fixture({ missing_processing_obligation: true })))
    mount()
    expect(await screen.findByRole('heading', { name: 'Preserved records need processing repair' })).toBeTruthy()
    expect(screen.getByText(/needs ADD operations review/)).toBeTruthy()
  })
  it.each(['http', 'wrong-device', 'future', 'missing-counts'])('keeps unverified counts unknown: %s', async kind => {
    const value = kind === 'wrong-device' ? fixture({ connector_id: 'different' })
      : kind === 'future' ? fixture({ sampled_at: new Date(Date.now() + 60_000).toISOString() })
        : kind === 'missing-counts' ? { enabled: true } : { detail: 'unavailable' }
    vi.mocked(fetch).mockResolvedValue(response(value, kind === 'http' ? 503 : 200))
    mount()
    expect(await screen.findByRole('heading', { name: 'Custody status unavailable' })).toBeTruthy()
    expect(screen.getByText(/Processing counts are unknown/)).toBeTruthy()
    expect(screen.queryByText('No journal processing groups recorded.')).toBeNull()
  })
  it('retains an older successful report when refresh fails and marks it historical', async () => {
    mount()
    await screen.findByRole('heading', { name: 'ADD custody processing' })
    vi.mocked(fetch).mockResolvedValue(response({}, 503))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh custody' }))
    expect(await screen.findByRole('heading', { name: 'Custody status unavailable' })).toBeTruthy()
    expect(screen.getByText(/Values below are the last report/)).toBeTruthy()
    expect(screen.getByText('12')).toBeTruthy()
  })
  it('rejects a slower previous request and a newer request carrying an older snapshot', async () => {
    let resolveFirst!: (value: Response) => void
    vi.mocked(fetch).mockReturnValueOnce(new Promise(resolve => { resolveFirst = resolve }))
    const latest = fixture({ counts: [{ state: 'WAIT_SOURCE', owner: 'ADD_RECONCILIATION', count: 7 }] })
    vi.mocked(fetch).mockResolvedValue(response(latest))
    const view = mount()
    view.rerender(<ZktCustodyStatus connectorId="fixture-connector" revision={1} />)
    await screen.findByText('7')
    await act(async () => { resolveFirst(response(fixture())); await Promise.resolve() })
    expect(screen.queryByText('12')).toBeNull()
    vi.mocked(fetch).mockResolvedValue(response(fixture({ sampled_at: new Date(Date.now() - 20_000).toISOString() })))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh custody' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    expect(screen.getByText('7')).toBeTruthy()
  })
  it('ages evidence even when no new device event arrives and polls after 30 seconds', async () => {
    vi.useFakeTimers()
    const prior = fixture()
    vi.mocked(fetch).mockImplementation(async () => response(prior))
    mount()
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(screen.getByRole('heading', { name: 'ADD custody processing' })).toBeTruthy()
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(fetch).toHaveBeenCalledTimes(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(16_000) })
    expect(screen.getByRole('heading', { name: 'Custody evidence is stale' })).toBeTruthy()
  })
  it('aborts a stalled read and removes its timers on unmount', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new Error('aborted')))
    }))
    const view = mount()
    await act(async () => { await vi.advanceTimersByTimeAsync(10_001) })
    expect(screen.getByRole('heading', { name: 'Custody status unavailable' })).toBeTruthy()
    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})
