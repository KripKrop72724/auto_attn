import { useEffect, useRef, useState } from 'react'

export const REVEAL_SECONDS = 60

/**
 * A shown raw record hides itself after 60 s, or at once when the tab goes to
 * the background. Returns the seconds left, for the visible countdown.
 */
export function useRevealTimer(shown: unknown, hide: () => void): number {
  const [left, setLeft] = useState(REVEAL_SECONDS)
  const hideRef = useRef(hide)
  hideRef.current = hide
  useEffect(() => {
    if (!shown) return
    setLeft(REVEAL_SECONDS)
    const started = Date.now()
    const tick = window.setInterval(() => setLeft(Math.max(0, REVEAL_SECONDS - Math.floor((Date.now() - started) / 1000))), 1000)
    const timeout = window.setTimeout(() => hideRef.current(), REVEAL_SECONDS * 1000)
    const onHidden = () => { if (document.hidden) hideRef.current() }
    document.addEventListener('visibilitychange', onHidden)
    return () => {
      window.clearInterval(tick)
      window.clearTimeout(timeout)
      document.removeEventListener('visibilitychange', onHidden)
    }
  }, [shown])
  return left
}
