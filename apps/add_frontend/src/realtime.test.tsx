import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useRealtime } from './realtime'
import { DeviceSnapshots } from './deviceData'
import type { Device } from './types'

class Stream extends EventTarget {
  static OPEN = 1
  static latest: Stream
  readyState = 1
  onopen: (() => void) | null = null
  onmessage: (() => void) | null = null
  onerror: (() => void) | null = null
  close = vi.fn()
  constructor() { super(); Stream.latest = this }
}

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })
describe('device refresh and stream recovery', () => {
  it('polls an OPEN but stalled stream, refreshes on focus, and keeps transport age separate', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', Stream)
    const topics = vi.fn()
    const { result, unmount } = renderHook(() => useRealtime(true, topics))
    act(() => { Stream.latest.onopen?.(); vi.advanceTimersByTime(120) })
    expect(topics).toHaveBeenLastCalledWith(new Set(['resync']))
    act(() => { Stream.latest.dispatchEvent(new Event('heartbeat')); vi.advanceTimersByTime(120) })
    expect(topics).toHaveBeenLastCalledWith(new Set(['device']))
    const transportAt = result.current.lastSyncAt
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(topics).toHaveBeenLastCalledWith(new Set(['resync']))
    expect(result.current.state).toBe('stale')
    expect(result.current.lastSyncAt).toEqual(transportAt)
    act(() => { window.dispatchEvent(new Event('focus')); vi.advanceTimersByTime(120) })
    expect(topics).toHaveBeenLastCalledWith(new Set(['resync']))
    unmount()
    expect(Stream.latest.close).toHaveBeenCalledOnce()
  })
  it('rejects a slow old fleet response after a fresh detail response', () => {
    const cache = new DeviceSnapshots()
    const old = { connector_id: 'one', snapshot_at: '2026-10-03T10:00:00Z', state: 'OFFLINE' } as Device
    const fresh = { ...old, snapshot_at: '2026-10-03T10:00:01Z', state: 'ONLINE' }
    cache.put([fresh])
    cache.put([old])
    expect(cache.all()[0]).toEqual(cache.get('one'))
    expect(cache.get('one')?.state).toBe('ONLINE')
    cache.clear()
    expect(cache.all()).toEqual([])
  })
  it('continues polling when EventSource is unavailable', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', undefined)
    const topics = vi.fn()
    renderHook(() => useRealtime(true, topics))
    act(() => { vi.advanceTimersByTime(30_120) })
    expect(topics).toHaveBeenCalledWith(new Set(['resync']))
  })
})
