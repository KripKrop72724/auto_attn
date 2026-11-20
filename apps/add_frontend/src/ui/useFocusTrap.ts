import { useEffect, useRef, type RefObject } from 'react'

export type InitialFocus = 'close' | 'first-field' | RefObject<HTMLElement | null>

// `details > summary` is focusable too (Technical details is often the last thing in a sheet).
const FOCUSABLE = 'button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"]), details > summary, [contenteditable="true"]'
const FIELD = 'input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled])'

export function focusableIn(node: HTMLElement | null) {
  const items = Array.from(node?.querySelectorAll<HTMLElement>(FOCUSABLE) || []).filter((element) => {
    if (element.closest('[inert]')) return false
    // Inside a closed <details> only its own summary can take focus.
    const closed = element.closest('details:not([open])')
    return !closed || (element.tagName === 'SUMMARY' && element.parentElement === closed)
  })
  // Leave out elements with no layout (inside a closed <details>, display: none),
  // so a hidden one never becomes first or last. Skipped when nothing has layout
  // (test DOMs do no layout).
  const shown = items.filter((element) => element.getClientRects().length > 0)
  return shown.length ? shown : items
}

// Moves focus into the layer when it opens, keeps Tab inside it while it is the
// innermost layer, and gives focus back when it closes.
export function useFocusTrap(
  containerRef: RefObject<HTMLElement | null>,
  {
    initialFocus = 'close',
    returnFocusRef,
    isTop = () => true,
  }: {
    initialFocus?: InitialFocus
    returnFocusRef?: RefObject<HTMLElement | null>
    isTop?: () => boolean
  } = {},
) {
  const options = useRef({ initialFocus, isTop })
  options.current = { initialFocus, isTop }
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const node = containerRef.current
    const focusFrame = window.requestAnimationFrame(() => {
      const wanted = options.current.initialFocus
      if (typeof wanted === 'object' && wanted?.current) {
        wanted.current.focus()
        return
      }
      if (wanted === 'first-field') {
        const field = Array.from(node?.querySelectorAll<HTMLElement>(FIELD) || []).find((element) => !element.closest('[inert]'))
        if (field) {
          field.focus()
          return
        }
      }
      focusableIn(node)[0]?.focus()
    })
    const handle = (event: KeyboardEvent) => {
      if (event.key !== 'Tab' || !options.current.isTop()) return
      const items = focusableIn(node)
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || !node?.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || !node?.contains(active))) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', handle)
    return () => {
      window.cancelAnimationFrame(focusFrame)
      document.removeEventListener('keydown', handle)
      const target = returnFocusRef?.current || previous
      target?.focus?.()
      // The app root is still inert while the layer's other cleanups (the modal
      // lock) have not run yet, so the first focus() is ignored. Try again once
      // the whole close has finished.
      if (target && document.activeElement !== target) {
        queueMicrotask(() => {
          if (target.isConnected && document.activeElement !== target) target.focus?.()
        })
      }
    }
  }, [containerRef, returnFocusRef])
}
