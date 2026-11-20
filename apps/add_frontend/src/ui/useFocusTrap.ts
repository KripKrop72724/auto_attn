import { useEffect, useRef, type RefObject } from 'react'

export type InitialFocus = 'close' | 'first-field' | RefObject<HTMLElement | null>

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
const FIELD = 'input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled])'

export function focusableIn(node: HTMLElement | null) {
  return Array.from(node?.querySelectorAll<HTMLElement>(FOCUSABLE) || []).filter((element) => !element.closest('[inert]'))
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
      ;(returnFocusRef?.current || previous)?.focus?.()
    }
  }, [containerRef, returnFocusRef])
}
