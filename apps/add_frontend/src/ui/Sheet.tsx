import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type PointerEvent, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from '../Icon'
import { DiscardConfirm } from './DiscardConfirm'
import { lockApplicationForModal, overlayRoot, playExitGhost } from './modalLock'
import { useDismiss } from './useDismiss'
import { useFocusTrap, type InitialFocus } from './useFocusTrap'
import { useKeyboardInset } from './useKeyboardInset'
import { useIsPhone } from './useMediaQuery'
import { UI_TEXT } from './uiText'
import { cx } from './cx'
import './sheet.css'

export type SheetProps = {
  title: string
  titleId?: string
  description?: ReactNode
  onClose: () => void
  children: ReactNode
  /** Sticky bottom bar for the sheet's buttons. */
  footer?: ReactNode
  /** Extra content under the title row (status line, tabs). */
  headerExtra?: ReactNode
  className?: string
  /** Default true: sheets hold details. Ignored while `dirty`. */
  dismissOnBackdrop?: boolean
  /** While true a backdrop tap and a swipe-down are ignored, Escape and Close ask first. */
  dirty?: boolean
  initialFocus?: InitialFocus
  returnFocusRef?: RefObject<HTMLElement | null>
  /** 'auto' = side panel from 761 px, bottom sheet up to 760 px. */
  placement?: 'auto' | 'side' | 'bottom'
}

const SWIPE_CLOSE_PX = 80

// One component for detail panels and phone sheets: a side panel from the
// right on wider screens (clip-path reveal, never past the screen edge), a
// bottom sheet with a handle on phones. Header / scrolling body / sticky footer.
export function Sheet({
  title,
  titleId: givenTitleId,
  description,
  onClose,
  children,
  footer,
  headerExtra,
  className,
  dismissOnBackdrop = true,
  dirty = false,
  initialFocus = 'close',
  returnFocusRef,
  placement = 'auto',
}: SheetProps) {
  const phone = useIsPhone()
  const bottom = placement === 'bottom' || (placement === 'auto' && phone)
  const generatedId = `sheet-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const titleId = givenTitleId || generatedId
  const backdropRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const [confirming, setConfirming] = useState(false)
  const drag = useRef<{ startY: number; dy: number } | null>(null)
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
    const isBottom = backdrop?.querySelector('.ui-sheet.is-bottom')
    return () => playExitGhost(backdrop, '.ui-sheet', isBottom
      ? [{ clipPath: 'inset(0)' }, { clipPath: 'inset(100% 0 0 0)' }]
      : [{ clipPath: 'inset(0)' }, { clipPath: 'inset(0 0 0 100%)' }])
  }, [bottom])

  const setOffset = (value: number) => {
    if (panelRef.current) panelRef.current.style.transform = value > 0 ? `translateY(${value}px)` : ''
  }
  const onHandleDown = (event: PointerEvent<HTMLDivElement>) => {
    if (!bottom) return
    drag.current = { startY: event.clientY, dy: 0 }
    event.currentTarget.setPointerCapture?.(event.pointerId)
  }
  const onHandleMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!drag.current || dirtyRef.current) return
    drag.current.dy = Math.max(0, event.clientY - drag.current.startY)
    setOffset(drag.current.dy)
  }
  const onHandleUp = () => {
    const moved = drag.current?.dy || 0
    drag.current = null
    setOffset(0)
    if (moved >= SWIPE_CLOSE_PX && !dirtyRef.current) closeRef.current()
  }

  return createPortal(
    <div
      ref={backdropRef}
      className={cx('ui-sheet-backdrop', bottom ? 'is-bottom' : 'is-side')}
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && dismissOnBackdrop && !dirtyRef.current) requestClose()
      }}
    >
      <div
        ref={panelRef}
        className={cx('ui-sheet', bottom ? 'is-bottom' : 'is-side', className)}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? `${titleId}-description` : undefined}
        data-placement={bottom ? 'bottom' : 'side'}
      >
        <div className="ui-layer-content" inert={confirming || undefined}>
          {bottom && (
            <div className="ui-sheet-handle" aria-hidden="true" onPointerDown={onHandleDown} onPointerMove={onHandleMove} onPointerUp={onHandleUp} onPointerCancel={onHandleUp}>
              <span />
            </div>
          )}
          <header className="ui-sheet-header">
            <div>
              <h2 id={titleId}>{title}</h2>
              {description && <p id={`${titleId}-description`}>{description}</p>}
            </div>
            <button className="icon-button ui-icon-button" type="button" onClick={requestClose} aria-label={UI_TEXT.close}>
              <Icon name="x" />
            </button>
          </header>
          {headerExtra}
          <div className="ui-sheet-body">{children}</div>
          {footer && <footer className="ui-sheet-footer">{footer}</footer>}
        </div>
        {confirming && <DiscardConfirm onKeep={() => setConfirming(false)} onDiscard={() => { setConfirming(false); closeRef.current() }} />}
      </div>
    </div>,
    overlayRoot(),
  )
}
