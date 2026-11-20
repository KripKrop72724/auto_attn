import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AdminAccessBanner, EndAdminAccessDialog, countdown } from './AdminAccessBanner'
import type { PeopleLease } from './model'

const lease = (extra: Partial<PeopleLease> = {}): PeopleLease => ({
  lease_id: 'lease-one', state: 'ACTIVE', requested_at: '2026-11-20T05:00:00Z', granted_at: '2026-11-20T05:00:10Z',
  expires_at: new Date(Date.now() + 462_000).toISOString(), revoked_at: null, last_error: null,
  user_id: '1007', user_display_name: 'Ayesha Khan', ...extra,
})

describe('Temporary admin access', () => {
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('counts down mm:ss', () => {
    expect(countdown('2026-11-20T05:10:00Z', Date.parse('2026-11-20T05:02:18Z'))).toBe('07:42')
    expect(countdown('2026-11-20T05:10:00Z', Date.parse('2026-11-20T05:11:00Z'))).toBe('00:00')
    expect(countdown(null, Date.now())).toBeNull()
  })

  it('names who has it and ticks every second', () => {
    vi.useFakeTimers({ now: Date.parse('2026-11-20T05:02:18Z') })
    render(<AdminAccessBanner lease={lease({ expires_at: '2026-11-20T05:10:00Z' })} onEnd={() => {}} />)
    expect(screen.getByRole('region', { name: 'Temporary admin access' }).textContent).toContain('Ayesha Khan (Employee ID 1007)')
    expect(screen.getByTestId('admin-access-line').textContent).toContain('Ends in 07:42')
    act(() => { vi.advanceTimersByTime(2000) })
    expect(screen.getByTestId('admin-access-line').textContent).toContain('Ends in 07:40')
  })

  it('says a failed or overdue access in words', () => {
    render(<AdminAccessBanner lease={lease({ state: 'OVERDUE' })} onEnd={() => {}} />)
    expect(screen.getByText('Should have ended. Waiting for the machine to confirm.')).toBeTruthy()
  })

  it('ends it with the same request as today, still after typing REVOKE ACCESS', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ command: { command_id: 'c', type: 'REVOKE_TEMP_ADMIN', status: 'QUEUED', created_at: '', expires_at: null } }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const onCommand = vi.fn()
    render(<EndAdminAccessDialog lease={lease()} deviceName="Building 9 · Device 3" onClose={() => {}} onCommand={onCommand} toast={{ notice: () => {} }} />)
    expect(screen.getByText('Ayesha Khan goes back to a regular person on Building 9 · Device 3.')).toBeTruthy()
    const end = screen.getByRole('button', { name: 'End now' }) as HTMLButtonElement
    expect(end.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText(/To confirm, type exactly/), { target: { value: 'REVOKE ACCESS' } })
    fireEvent.click(end)
    await waitFor(() => expect(onCommand).toHaveBeenCalled())
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/v1/admin-leases/lease-one/revoke')
    expect(init.method).toBe('POST')
    expect(init.body).toBe('{}')
  })
})
