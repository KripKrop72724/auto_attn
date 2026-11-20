import { useEffect, useRef, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from '../Icon'

let modalLockDepth = 0
let lockedWorkspace: HTMLElement | null = null
let lockedWorkspaceOverflow = ''
let lockedWorkspaceScrollTop = 0
let rootAriaHidden: string | null = null
let rootWasInert = false

function lockApplicationForModal() {
  const root = document.getElementById('root')
  if (modalLockDepth === 0) {
    lockedWorkspace = document.querySelector<HTMLElement>('.app-workspace')
    lockedWorkspaceOverflow = lockedWorkspace?.style.overflow || ''
    lockedWorkspaceScrollTop = lockedWorkspace?.scrollTop || 0
    if (lockedWorkspace) lockedWorkspace.style.overflow = 'hidden'
    if (root) {
      rootAriaHidden = root.getAttribute('aria-hidden')
      rootWasInert = root.hasAttribute('inert')
      root.setAttribute('aria-hidden', 'true')
      root.setAttribute('inert', '')
    }
  }
  modalLockDepth += 1
  return () => {
    modalLockDepth = Math.max(0, modalLockDepth - 1)
    if (modalLockDepth > 0) return
    if (lockedWorkspace) {
      lockedWorkspace.style.overflow = lockedWorkspaceOverflow
      lockedWorkspace.scrollTop = lockedWorkspaceScrollTop
    }
    if (root) {
      if (rootAriaHidden === null) root.removeAttribute('aria-hidden')
      else root.setAttribute('aria-hidden', rootAriaHidden)
      if (!rootWasInert) root.removeAttribute('inert')
    }
    lockedWorkspace = null
  }
}

// Open dialogs, innermost last: a dialog opened from another one (for example
// an operator action inside the device drawer) owns Escape and the focus trap.
const openDialogs: symbol[] = []

export function Dialog({
  titleId,
  title,
  description,
  onClose,
  children,
  className = '',
  returnFocusRef,
}: {
  titleId: string
  title: string
  description?: string
  onClose: () => void
  children: ReactNode
  className?: string
  returnFocusRef?: RefObject<HTMLElement | null>
}) {
  const panel = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const releaseModalLock = lockApplicationForModal()
    const token = Symbol('dialog')
    openDialogs.push(token)
    const node = panel.current
    const focusable = () =>
      Array.from(
        node?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
        ) || [],
      )
    const focusFrame = window.requestAnimationFrame(() => focusable()[0]?.focus())
    const handle = (event: KeyboardEvent) => {
      if (openDialogs[openDialogs.length - 1] !== token) return
      if (event.key === 'Escape') {
        event.preventDefault()
        closeRef.current()
      }
      if (event.key === 'Tab') {
        const items = focusable()
        if (!items.length) return
        const first = items[0]
        const last = items[items.length - 1]
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault()
          last.focus()
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', handle)
    return () => {
      const position = openDialogs.indexOf(token)
      if (position >= 0) openDialogs.splice(position, 1)
      window.cancelAnimationFrame(focusFrame)
      document.removeEventListener('keydown', handle)
      releaseModalLock()
      ;(returnFocusRef?.current || previous)?.focus()
    }
  }, [returnFocusRef])
  return createPortal(
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <div
        ref={panel}
        className={`dialog-panel ${className}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? `${titleId}-description` : undefined}
      >
        <header className="dialog-header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description && <p id={`${titleId}-description`}>{description}</p>}
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close dialog">
            <Icon name="x" />
          </button>
        </header>
        {children}
      </div>
    </div>,
    document.getElementById('overlay-root') || document.body,
  )
}
