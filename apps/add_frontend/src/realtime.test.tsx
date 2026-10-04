import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useRealtime } from './realtime'
import { DeviceSnapshots } from './deviceData'
import type { Device } from './types'

class Stream extends EventTarget {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSED = 2
  static latest: Stream
  static instances: Stream[] = []
  readyState = 1
  onopen: (() => void) | null = null
  onmessage: (() => void) | null = null
  onerror: (() => void) | null = null
  close = vi.fn(() => { this.readyState = Stream.CLOSED })
  constructor(readonly url: string, readonly options: EventSourceInit) {
    super()
    Stream.latest = this
    Stream.instances.push(this)
  }
}

beforeEach(() => { Stream.instances = [] })
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
  it('replaces a permanently closed connection and resynchronizes on its new open event', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', Stream)
    const topics = vi.fn()
    const { result, unmount } = renderHook(() => useRealtime(true, topics))
    const closed = Stream.latest
    act(() => { closed.readyState = Stream.CLOSED; closed.onerror?.() })
    expect(result.current.state).toBe('reconnecting')
    act(() => { vi.advanceTimersByTime(30_120) })
    expect(Stream.instances).toHaveLength(2)
    expect(closed.close).toHaveBeenCalledOnce()
    expect(Stream.latest.url).toBe('/events/v1/stream')
    expect(Stream.latest.options).toEqual({ withCredentials: true })
    expect(result.current.state).toBe('stale')
    expect(result.current.lastSyncAt).toBeNull()
    topics.mockClear()
    act(() => { Stream.latest.onopen?.(); vi.advanceTimersByTime(120) })
    expect(result.current.state).toBe('live')
    expect(result.current.lastSyncAt).not.toBeNull()
    expect(topics).toHaveBeenCalledExactlyOnceWith(new Set(['resync']))
    unmount()
    expect(Stream.latest.close).toHaveBeenCalledOnce()
  })
  it.each([Stream.OPEN, Stream.CONNECTING])('replaces a silent stream in state %i without refreshing its evidence age', (readyState) => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', Stream)
    const topics = vi.fn()
    const { result } = renderHook(() => useRealtime(true, topics))
    const stalled = Stream.latest
    act(() => { stalled.onopen?.(); stalled.readyState = readyState })
    const transportAt = result.current.lastSyncAt
    act(() => { vi.advanceTimersByTime(30_120) })
    expect(Stream.instances).toHaveLength(1)
    expect(result.current.state).toBe('stale')
    act(() => { vi.advanceTimersByTime(30_000) })
    expect(Stream.instances).toHaveLength(2)
    expect(stalled.close).toHaveBeenCalledOnce()
    expect(result.current.lastSyncAt).toEqual(transportAt)
    expect(result.current.state).toBe('stale')
    expect(topics).toHaveBeenLastCalledWith(new Set(['resync']))
  })
  it('keeps a stream with regular keepalives and still polls snapshots', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', Stream)
    const topics = vi.fn()
    const { result } = renderHook(() => useRealtime(true, topics))
    act(() => { Stream.latest.onopen?.() })
    for (let pulse = 0; pulse < 9; pulse += 1) {
      act(() => {
        vi.advanceTimersByTime(20_000)
        Stream.latest.dispatchEvent(new Event('keepalive'))
      })
    }
    expect(Stream.instances).toHaveLength(1)
    expect(Stream.latest.close).not.toHaveBeenCalled()
    expect(result.current.state).toBe('live')
    expect(topics).toHaveBeenCalledWith(new Set(['resync']))
  })
  it('ignores callbacks from a replaced connection and after unmount', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', Stream)
    const topics = vi.fn()
    const { result, unmount } = renderHook(() => useRealtime(true, topics))
    const old = Stream.latest
    old.readyState = Stream.CLOSED
    act(() => { vi.advanceTimersByTime(30_120); Stream.latest.onopen?.(); vi.advanceTimersByTime(120) })
    const transportAt = result.current.lastSyncAt
    topics.mockClear()
    act(() => {
      vi.advanceTimersByTime(1_000)
      old.onopen?.()
      old.onmessage?.()
      old.onerror?.()
      old.dispatchEvent(new Event('heartbeat'))
      old.dispatchEvent(new Event('keepalive'))
      vi.advanceTimersByTime(120)
    })
    expect(result.current.state).toBe('live')
    expect(result.current.lastSyncAt).toEqual(transportAt)
    expect(topics).not.toHaveBeenCalled()
    unmount()
    act(() => {
      Stream.latest.onopen?.()
      Stream.latest.onmessage?.()
      Stream.latest.dispatchEvent(new Event('heartbeat'))
      window.dispatchEvent(new Event('focus'))
      vi.advanceTimersByTime(90_000)
    })
    expect(topics).not.toHaveBeenCalled()
    expect(Stream.instances).toHaveLength(2)
    expect(vi.getTimerCount()).toBe(0)
  })
  it('bounds connection attempts even when focus and visibility events repeat', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', Stream)
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    renderHook(() => useRealtime(true, vi.fn()))
    Stream.latest.readyState = Stream.CLOSED
    act(() => { vi.advanceTimersByTime(30_000) })
    Stream.latest.readyState = Stream.CLOSED
    for (let attempt = 0; attempt < 100; attempt += 1) {
      act(() => {
        window.dispatchEvent(new Event('focus'))
        document.dispatchEvent(new Event('visibilitychange'))
      })
    }
    expect(Stream.instances).toHaveLength(2)
    act(() => { vi.advanceTimersByTime(30_000) })
    expect(Stream.instances).toHaveLength(3)
    expect(Stream.instances.slice(0, -1).every((stream) => stream.close.mock.calls.length === 1)).toBe(true)
    vi.restoreAllMocks()
  })
  it('polls and retries when the browser cannot construct EventSource', () => {
    vi.useFakeTimers()
    const construct = vi.fn()
    vi.stubGlobal('EventSource', class {
      constructor() { construct(); throw new Error('unavailable') }
    })
    const topics = vi.fn()
    const { result } = renderHook(() => useRealtime(true, topics))
    act(() => { vi.advanceTimersByTime(60_120) })
    expect(construct).toHaveBeenCalledTimes(3)
    expect(topics).toHaveBeenCalledTimes(2)
    expect(result.current.state).toBe('stale')
    expect(result.current.lastSyncAt).toBeNull()
  })
  it('stops recovery when disabled and starts with fresh evidence when enabled again', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', Stream)
    const topics = vi.fn()
    const { result, rerender } = renderHook(({ enabled }) => useRealtime(enabled, topics), { initialProps: { enabled: true } })
    const old = Stream.latest
    act(() => { old.onopen?.(); vi.advanceTimersByTime(120) })
    expect(result.current.lastSyncAt).not.toBeNull()
    rerender({ enabled: false })
    topics.mockClear()
    act(() => { vi.advanceTimersByTime(90_000); old.onmessage?.(); vi.advanceTimersByTime(120) })
    expect(Stream.instances).toHaveLength(1)
    expect(topics).not.toHaveBeenCalled()
    expect(old.close).toHaveBeenCalledOnce()
    rerender({ enabled: true })
    expect(Stream.instances).toHaveLength(2)
    expect(result.current.state).toBe('connecting')
    expect(result.current.lastSyncAt).toBeNull()
  })
  it('rejects a slow old fleet response after a fresh detail response', () => {
    const cache = new DeviceSnapshots()
    const old = { connector_id: 'one', snapshot_at: '2026-10-03T10:00:00Z', state: 'OFFLINE' } as Device
    const fresh = { ...old, snapshot_at: '2026-10-03T10:00:01Z', state: 'ONLINE' }
    cache.put([fresh])
    cache.replaceFleet([old])
    expect(cache.all()[0]).toEqual(cache.get('one'))
    expect(cache.get('one')?.state).toBe('ONLINE')
    cache.clear()
    expect(cache.all()).toEqual([])
  })
  it('removes a connector from fleet membership while retaining its last detail snapshot', () => {
    const cache = new DeviceSnapshots()
    const row = { connector_id: 'one', snapshot_at: '2026-10-03T10:00:00Z' } as Device
    cache.replaceFleet([row])
    expect(cache.all()).toHaveLength(1)
    cache.replaceFleet([])
    cache.put([{ ...row, snapshot_at: '2026-10-03T10:00:01Z' }])
    expect(cache.all()).toEqual([])
    expect(cache.get('one')).toBeDefined()
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
