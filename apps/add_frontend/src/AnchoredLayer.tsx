import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'

export type AnchoredLayerDismissReason = 'escape' | 'outside'

type LayerPosition = {
  ready: boolean
  placement: 'top' | 'bottom'
  top: number
  left: number
  width: number
  maxHeight: number
  mobile: boolean
}

const initialPosition: LayerPosition = {
  ready: false,
  placement: 'bottom',
  top: 0,
  left: 0,
  width: 0,
  maxHeight: 0,
  mobile: false,
}

const overlayRoot = () => document.getElementById('overlay-root') || document.body

export function AnchoredLayer({
  anchorRef,
  children,
  className = '',
  matchAnchor = false,
  mobileSheet = false,
  preferredWidth = 320,
  onDismiss,
}: {
  anchorRef: RefObject<HTMLElement | null>
  children: ReactNode
  className?: string
  matchAnchor?: boolean
  mobileSheet?: boolean
  preferredWidth?: number
  onDismiss: (reason: AnchoredLayerDismissReason) => void
}) {
  const layerRef = useRef<HTMLDivElement>(null)
  const [position, setPosition] = useState<LayerPosition>(initialPosition)

  const updatePosition = useCallback(() => {
    const anchor = anchorRef.current
    const layer = layerRef.current
    if (!anchor || !layer) return

    const visualViewport = window.visualViewport
    const viewportTop = visualViewport?.offsetTop || 0
    const viewportLeft = visualViewport?.offsetLeft || 0
    const viewportWidth = visualViewport?.width || window.innerWidth
    const viewportHeight = visualViewport?.height || window.innerHeight
    const viewportRight = viewportLeft + viewportWidth
    const viewportBottom = viewportTop + viewportHeight
    const mobile = mobileSheet && (typeof window.matchMedia === 'function'
      ? window.matchMedia('(max-width: 760px)').matches
      : window.innerWidth <= 760)

    if (mobile) {
      setPosition({
        ready: true,
        placement: 'bottom',
        top: viewportBottom,
        left: viewportLeft,
        width: viewportWidth,
        maxHeight: Math.max(240, viewportHeight - 86),
        mobile: true,
      })
      return
    }

    const margin = 12
    const gap = 8
    const anchorBox = anchor.getBoundingClientRect()
    const below = viewportBottom - anchorBox.bottom - gap - margin
    const above = anchorBox.top - viewportTop - gap - margin
    const placement = below >= Math.min(320, above) || below >= above ? 'bottom' : 'top'
    const available = Math.max(180, placement === 'bottom' ? below : above)
    const width = Math.min(
      Math.max(preferredWidth, matchAnchor ? anchorBox.width : 0),
      viewportWidth - margin * 2,
    )
    const naturalHeight = Math.min(layer.scrollHeight || available, available)
    const unclampedLeft = matchAnchor ? anchorBox.left : anchorBox.right - width
    const left = Math.max(
      viewportLeft + margin,
      Math.min(unclampedLeft, viewportRight - margin - width),
    )
    const top = placement === 'bottom'
      ? anchorBox.bottom + gap
      : anchorBox.top - gap - naturalHeight

    setPosition({
      ready: true,
      placement,
      top: Math.max(viewportTop + margin, top),
      left,
      width,
      maxHeight: available,
      mobile: false,
    })
  }, [anchorRef, matchAnchor, mobileSheet, preferredWidth])

  useLayoutEffect(() => {
    updatePosition()
    const anchor = anchorRef.current
    const layer = layerRef.current
    const observer = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(updatePosition)
    if (anchor) observer?.observe(anchor)
    if (layer) observer?.observe(layer)

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null
      if (target && !layerRef.current?.contains(target) && !anchorRef.current?.contains(target)) {
        onDismiss('outside')
      }
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopPropagation()
      onDismiss('escape')
    }
    const viewport = window.visualViewport
    document.addEventListener('pointerdown', handlePointerDown, true)
    document.addEventListener('keydown', handleKeyDown, true)
    window.addEventListener('resize', updatePosition)
    window.addEventListener('scroll', updatePosition, true)
    viewport?.addEventListener('resize', updatePosition)
    viewport?.addEventListener('scroll', updatePosition)
    return () => {
      observer?.disconnect()
      document.removeEventListener('pointerdown', handlePointerDown, true)
      document.removeEventListener('keydown', handleKeyDown, true)
      window.removeEventListener('resize', updatePosition)
      window.removeEventListener('scroll', updatePosition, true)
      viewport?.removeEventListener('resize', updatePosition)
      viewport?.removeEventListener('scroll', updatePosition)
    }
  }, [anchorRef, onDismiss, updatePosition])

  const style = {
    top: position.mobile ? undefined : position.top,
    left: position.mobile ? undefined : position.left,
    width: position.mobile ? undefined : position.width,
    visibility: position.ready ? 'visible' : 'hidden',
    '--anchored-max-height': `${position.maxHeight}px`,
  } as CSSProperties

  return createPortal(
    <div
      ref={layerRef}
      className={`anchored-layer ${position.mobile ? 'is-mobile-sheet' : ''} ${className}`.trim()}
      data-placement={position.placement}
      style={style}
    >
      {children}
    </div>,
    overlayRoot(),
  )
}
