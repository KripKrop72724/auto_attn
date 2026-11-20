import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Command } from '../types'
import { canCancelCommand, commandStep, isFinalCommand, useCommandTracker } from './useCommandTracker'

const command = (status: string): Command => ({
  command_id: 'cmd-1', type: 'REFRESH_USERS', status, created_at: '2026-11-06T05:00:00Z', expires_at: null,
})
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('command steps', () => {
  it('maps every backend status to Waiting, Sent to machine or an end', () => {
    for (const status of ['QUEUED', 'WAITING_FOR_DEVICE', 'WAITING_FOR_ZKT', 'RETRYING', 'SOMETHING_NEW']) expect(commandStep(command(status))).toBe('waiting')
    for (const status of ['DISPATCHED', 'ACKNOWLEDGED', 'RUNNING', 'CANCEL_REQUESTED']) expect(commandStep(command(status))).toBe('sent')
    expect(commandStep(command('SUCCEEDED'))).toBe('confirmed')
    expect(commandStep(command('FAILED'))).toBe('failed')
    expect(commandStep(command('EXPIRED'))).toBe('failed')
    expect(commandStep(command('CANCELED'))).toBe('cancelled')
    expect(isFinalCommand(command('CANCELLED'))).toBe(true)
    expect(isFinalCommand(command('RUNNING'))).toBe(false)
    expect(canCancelCommand(command('QUEUED'))).toBe(true)
    expect(canCancelCommand(command('RUNNING'))).toBe(false)
  })
})

describe('useCommandTracker', () => {
  it('polls until the change ends, checks at once on a live event and reports the end once', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const answers = [command('DISPATCHED'), command('RUNNING'), command('SUCCEEDED')]
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => json(answers.shift() || command('SUCCEEDED')))
    vi.stubGlobal('fetch', fetchMock)
    const onFinal = vi.fn()
    const { result, rerender } = renderHook(({ revision }) => useCommandTracker(command('QUEUED'), revision, onFinal), { initialProps: { revision: 0 } })
    expect(result.current.step).toBe('waiting')
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    await waitFor(() => expect(result.current.step).toBe('sent'))
    rerender({ revision: 1 })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    await waitFor(() => expect(result.current.step).toBe('confirmed'))
    expect(result.current.final).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(onFinal).toHaveBeenCalledOnce()
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/v2/commands/cmd-1')
  })

  it('keeps polling after a failed check and cancels with one request', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') return json(command('CANCELLED'))
      return fetchMock.mock.calls.length === 1 ? new Response('{}', { status: 502 }) : json(command('QUEUED'))
    })
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useCommandTracker('cmd-1'))
    await waitFor(() => expect(result.current.checkFailed).toBe(true))
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    await waitFor(() => expect(result.current.checkFailed).toBe(false))
    await act(async () => { await result.current.cancel() })
    expect(result.current.step).toBe('cancelled')
    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(posts).toHaveLength(1)
    expect(String(posts[0][0])).toBe('/api/v2/commands/cmd-1/cancel')
  })
})
