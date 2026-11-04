import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createThrottledRunner } from './throttle'

const deferred = () => {
  let resolve!: () => void
  const promise = new Promise<void>((done) => { resolve = done })
  return { promise, resolve }
}

describe('throttled runner', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-04T09:00:00Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('coalesces a burst into one leading run and one trailing run per interval', async () => {
    const task = vi.fn(async () => undefined)
    const runner = createThrottledRunner(task, 3000)

    runner.schedule()
    await vi.advanceTimersByTimeAsync(0)
    expect(task).toHaveBeenCalledTimes(1)

    for (let index = 0; index < 50; index += 1) {
      runner.schedule()
      await vi.advanceTimersByTimeAsync(20)
    }
    expect(task).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(3000)
    expect(task).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(task).toHaveBeenCalledTimes(2)
  })

  it('never starts a run while one is in flight and picks the change up afterwards', async () => {
    const gates: Array<ReturnType<typeof deferred>> = []
    const task = vi.fn(() => {
      const gate = deferred()
      gates.push(gate)
      return gate.promise
    })
    const runner = createThrottledRunner(task, 15_000)

    runner.schedule()
    await vi.advanceTimersByTimeAsync(0)
    expect(task).toHaveBeenCalledTimes(1)

    // The request is slow (an overloaded backend); events keep arriving.
    runner.schedule()
    await vi.advanceTimersByTimeAsync(40_000)
    runner.schedule()
    expect(task).toHaveBeenCalledTimes(1)

    gates[0].resolve()
    await vi.advanceTimersByTimeAsync(0)
    // Fifteen seconds already passed since the first start: run the trailing refresh now.
    expect(task).toHaveBeenCalledTimes(2)
    gates[1].resolve()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(task).toHaveBeenCalledTimes(2)
  })

  it('runs an explicit refresh immediately, or once right after the run in flight', async () => {
    const gates: Array<ReturnType<typeof deferred>> = []
    const task = vi.fn(() => {
      const gate = deferred()
      gates.push(gate)
      return gate.promise
    })
    const runner = createThrottledRunner(task, 15_000)

    const first = runner.runNow()
    expect(task).toHaveBeenCalledTimes(1)
    const second = runner.runNow()
    const third = runner.runNow()
    expect(second).toBe(third)
    expect(task).toHaveBeenCalledTimes(1)

    gates[0].resolve()
    await first
    await vi.advanceTimersByTimeAsync(0)
    expect(task).toHaveBeenCalledTimes(2)
    gates[1].resolve()
    await second

    // An explicit refresh ignores the interval.
    const fourth = runner.runNow()
    expect(task).toHaveBeenCalledTimes(3)
    gates[2].resolve()
    await fourth
  })

  it('uses the current interval and pulls a pending refresh earlier when it shrinks', async () => {
    let interval = 60_000
    const task = vi.fn(async () => undefined)
    const runner = createThrottledRunner(task, () => interval)

    runner.schedule()
    await vi.advanceTimersByTimeAsync(0)
    runner.schedule()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(task).toHaveBeenCalledTimes(1)

    interval = 15_000
    runner.schedule()
    await vi.advanceTimersByTimeAsync(4_999)
    expect(task).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(task).toHaveBeenCalledTimes(2)
  })

  it('keeps working after a failed run and drops pending work on cancel', async () => {
    const task = vi.fn(async () => { throw new Error('offline') })
    const runner = createThrottledRunner(task, 1000)

    await runner.runNow()
    runner.schedule()
    runner.cancel()
    await vi.advanceTimersByTimeAsync(5000)
    expect(task).toHaveBeenCalledTimes(1)

    runner.schedule()
    await vi.advanceTimersByTimeAsync(0)
    expect(task).toHaveBeenCalledTimes(2)
  })
})
