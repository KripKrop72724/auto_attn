import { useEffect } from 'react'

// Keeps sticky footers above the phone keyboard: sets --keyboard-inset on the
// document from the visual viewport while a form layer is open.
export function useKeyboardInset(enabled = true) {
  useEffect(() => {
    const viewport = window.visualViewport
    if (!enabled || !viewport) return
    const root = document.documentElement
    const update = () => {
      const inset = Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop)
      root.style.setProperty('--keyboard-inset', `${Math.round(inset)}px`)
    }
    update()
    viewport.addEventListener('resize', update)
    viewport.addEventListener('scroll', update)
    return () => {
      viewport.removeEventListener('resize', update)
      viewport.removeEventListener('scroll', update)
      root.style.removeProperty('--keyboard-inset')
    }
  }, [enabled])
}
