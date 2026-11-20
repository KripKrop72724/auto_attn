import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from '../Icon'
import { DiscardConfirm } from './DiscardConfirm'
import { lockApplicationForModal, overlayRoot, playExitGhost } from './modalLock'
import { useDismiss } from './useDismiss'
import { useFocusTrap, type InitialFocus } from './useFocusTrap'
import { useKeyboardInset } from './useKeyboardInset'
import { UI_TEXT } from './uiText'
import { cx } from './cx'

export type DialogSize = 'sm' | 'md' | 'lg'

export type DialogProps = {
  titleId: string
  title: string
  description?: string
  onClose: () => void
  children: ReactNode
  className?: string
  returnFocusRef?: RefObject<HTMLElement | null>
  /** 440 / 640 / 840 px. Default md (today's width). */
  size?: DialogSize
  /** Default false: a click outside does nothing; Escape and Close still work. */
  dismissOnBackdrop?: boolean
  /** When true, Escape and Close ask "Discard your changes?" first. */
  dirty?: boolean
  /** Default 'close' (the header Close button), 'first-field', or a ref. */
  initialFocus?: InitialFocus
}

// The one modal dialog. Portals into #overlay-root, makes #root inert and
// hidden, locks the workspace scroll and puts it back, and lets only the
// innermost layer handle Escape and Tab. It never adds an "Are you sure?" of
// its own: the only question it asks is about losing typed text (`dirty`).
export function Dialog({
  titleId,
  title,
  description,
  onClose,
  children,
  className = '',
  returnFocusRef,
  size = 'md',
  dismissOnBackdrop = false,
  dirty = false,
  initialFocus = 'close',
}: DialogProps) {
  const backdropRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const [confirming, setConfirming] = useState(false)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  const dirtyRef = useRef(dirty)
  dirtyRef.current = dirty

  const requestClose = useCallback(() => {
    if (dirtyRef.current) setConfirming(true)
    else closeRef.current()
  }, [])
  const isTop = useDismiss(() => {
    if (confirming) setConfirming(false)
    else requestClose()
  })
  useFocusTrap(panelRef, { initialFocus, returnFocusRef, isTop })
  useKeyboardInset()
  useEffect(() => lockApplicationForModal(), [])
  useLayoutEffect(() => {
    const backdrop = backdropRef.current
    return () => playExitGhost(backdrop, '.dialog-panel', [
      { opacity: 1, transform: 'translateY(0)' },
      { opacity: 0, transform: 'translateY(8px)' },
    ])
  }, [])

  return createPortal(
    <div
      ref={backdropRef}
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && dismissOnBackdrop && !dirtyRef.current) requestClose()
      }}
    >
      <div
        ref={panelRef}
        className={cx('dialog-panel', `dialog-size-${size}`, className)}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? `${titleId}-description` : undefined}
      >
        <div className="ui-layer-content" inert={confirming || undefined}>
          <header className="dialog-header">
            <div>
              <h2 id={titleId}>{title}</h2>
              {description && <p id={`${titleId}-description`}>{description}</p>}
            </div>
            <button className="icon-button ui-icon-button" type="button" onClick={requestClose} aria-label={UI_TEXT.close}>
              <Icon name="x" />
            </button>
          </header>
          {children}
        </div>
        {confirming && <DiscardConfirm onKeep={() => setConfirming(false)} onDiscard={() => { setConfirming(false); closeRef.current() }} />}
      </div>
    </div>,
    overlayRoot(),
  )
}
