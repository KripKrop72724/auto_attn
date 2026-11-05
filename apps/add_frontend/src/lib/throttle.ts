import { useEffect, useRef, useState } from 'react'

// Realtime topics arrive on every heartbeat, punch batch and reconcile chunk.
// A runner turns that stream into bounded, non-overlapping requests, so a
// 22k-punch release or a 75k-record reconcile cannot make every open dashboard
// re-read heavy summaries once per event.
export type ThrottledRunner = {
  /**
   * Automatic refresh. Starts at most once per interval (trailing edge, so the
   * last change is always picked up) and never while a run is in flight.
   */
  schedule: () => void
  /**
   * Explicit refresh (a button, a just-finished action). Starts now, or once
   * right after the run in flight. Resolves when that run settles.
   */
  runNow: () => Promise<void>
  /** Drops pending and follow-up work. Later calls still work. */
  cancel: () => void
}

type Interval = number | (() => number)

export function createThrottledRunner(
  task: () => unknown,
  intervalMs: Interval,
  now: () => number = Date.now,
): ThrottledRunner {
  let inFlight: Promise<void> | null = null
  let forced: Promise<void> | null = null
  let lastStart = Number.NEGATIVE_INFINITY
  let timer: ReturnType<typeof setTimeout> | null = null
  let timerDue = 0
  let dirty = false
  let generation = 0
  const interval = () => Math.max(0, typeof intervalMs === 'function' ? intervalMs() : intervalMs)
  const clearTimer = () => {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }

  const start = (): Promise<void> => {
    clearTimer()
    dirty = false
    lastStart = now()
    const startedIn = generation
    const run = (async () => {
      try {
        await task()
      } catch {
        // The task reports its own failures; a rejection must not wedge the runner.
      }
    })()
    inFlight = run
    void run.then(() => {
      if (inFlight === run) inFlight = null
      if (startedIn !== generation || !dirty) return
      dirty = false
      schedule()
    })
    return run
  }

  const schedule = () => {
    if (inFlight || forced) {
      dirty = true
      return
    }
    const due = Math.max(now(), lastStart + interval())
    // A shorter interval (for example returning to the Fleet view) may pull a
    // pending refresh earlier; it never pushes one later.
    if (timer !== null && timerDue <= due) return
    clearTimer()
    timerDue = due
    timer = setTimeout(() => {
      timer = null
      if (inFlight || forced) dirty = true
      else void start()
    }, due - now())
  }

  const runNow = (): Promise<void> => {
    if (forced) return forced
    if (!inFlight) return start()
    const requestedIn = generation
    const follow = inFlight.then(() => {
      if (forced === follow) forced = null
      return requestedIn === generation ? start() : undefined
    })
    forced = follow
    return follow
  }

  const cancel = () => {
    generation += 1
    clearTimer()
    dirty = false
    forced = null
  }

  return { schedule, runNow, cancel }
}

// One runner per component instance. The latest task and interval are always
// used, so callers may pass inline callbacks.
export function useThrottledRunner(task: () => unknown, intervalMs: Interval): ThrottledRunner {
  const taskRef = useRef(task)
  const intervalRef = useRef(intervalMs)
  taskRef.current = task
  intervalRef.current = intervalMs
  const [runner] = useState(() => createThrottledRunner(
    () => taskRef.current(),
    () => {
      const value = intervalRef.current
      return typeof value === 'function' ? value() : value
    },
  ))
  useEffect(() => () => runner.cancel(), [runner])
  return runner
}
