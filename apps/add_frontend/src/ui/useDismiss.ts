import { useCallback, useEffect, useRef } from 'react'
import { isTopLayer, pushLayer } from './modalLock'

// Registers a layer in the shared stack and calls onEscape when Escape is
// pressed while this layer is the innermost one. Returns isTop() for the focus
// trap, so only the innermost layer traps Tab.
export function useDismiss(onEscape: () => void, { enabled = true }: { enabled?: boolean } = {}) {
  const tokenRef = useRef<symbol>(Symbol('layer'))
  const escapeRef = useRef(onEscape)
  escapeRef.current = onEscape
  useEffect(() => {
    if (!enabled) return
    const token = tokenRef.current
    const release = pushLayer(token)
    const handle = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || !isTopLayer(token)) return
      event.preventDefault()
      escapeRef.current()
    }
    document.addEventListener('keydown', handle)
    return () => {
      document.removeEventListener('keydown', handle)
      release()
    }
  }, [enabled])
  return useCallback(() => isTopLayer(tokenRef.current), [])
}
