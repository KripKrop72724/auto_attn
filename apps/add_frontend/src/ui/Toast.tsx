import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { Icon } from '../Icon'
import { overlayRoot } from './modalLock'
import { UI_TEXT } from './uiText'
import { cx } from './cx'

export type ToastTone = 'success' | 'info' | 'error'

export type ToastInput = {
  tone: ToastTone
  text: string
  /** One button in the message, e.g. "Undo", "Try again", "View". */
  action?: { label: string; onClick: () => void }
  /** Milliseconds before a success / info message closes (default 5000). Errors stay until closed. */
  duration?: number
}

export type ToastItem = ToastInput & { id: number }

/** The old single-message shape, kept for the 36 feature files and two test harnesses. */
export type LegacyToast = { kind: 'notice' | 'error'; text: string } | null

export type ToastApi = {
  /** Newest message in the old shape. */
  toast: LegacyToast
  notice: (text: string) => void
  error: (text: string) => void
  /** Closes the newest message (old behaviour), or the one with `id`. */
  dismiss: (id?: number) => void
  show: (input: ToastInput) => number
  items: ToastItem[]
}

const MAX_TOASTS = 3
const DEFAULT_DURATION = 5000

let nextToastId = 1

function useToastQueue(): ToastApi {
  const [items, setItems] = useState<ToastItem[]>([])
  const show = useCallback((input: ToastInput) => {
    const id = nextToastId++
    setItems((current) => [...current, { ...input, id }].slice(-MAX_TOASTS))
    return id
  }, [])
  const dismiss = useCallback((id?: number) => {
    setItems((current) => id === undefined ? current.slice(0, -1) : current.filter((item) => item.id !== id))
  }, [])
  const notice = useCallback((text: string) => { show({ tone: 'success', text }) }, [show])
  const error = useCallback((text: string) => { show({ tone: 'error', text }) }, [show])
  return useMemo(() => {
    const newest = items[items.length - 1]
    const toast: LegacyToast = newest ? { kind: newest.tone === 'error' ? 'error' : 'notice', text: newest.text } : null
    return { toast, notice, error, dismiss, show, items }
  }, [dismiss, error, items, notice, show])
}

const ToastContext = createContext<ToastApi | null>(null)

// Holds the messages for the whole app and shows them in #overlay-root, so they
// are announced while #root is inert behind a dialog.
export function ToastProvider({ children }: { children: ReactNode }) {
  const api = useToastQueue()
  return (
    <ToastContext.Provider value={api}>
      {children}
      <ToastViewport items={api.items} onDismiss={api.dismiss} />
    </ToastContext.Provider>
  )
}

// Same return shape as the old App.tsx hook ({ toast, notice, error, dismiss })
// plus show() and items. Inside ToastProvider it uses the shared queue; outside
// one (unit-test harnesses) it keeps its own queue and the caller renders it.
export function useToast(): ToastApi {
  const shared = useContext(ToastContext)
  const local = useToastQueue()
  return shared || local
}

export function ToastViewport({ items, onDismiss }: { items: ToastItem[]; onDismiss: (id: number) => void }) {
  if (!items.length) return null
  return createPortal(
    <div className="ui-toast-stack">
      {items.map((item) => <ToastMessage key={item.id} item={item} onDismiss={onDismiss} />)}
    </div>,
    overlayRoot(),
  )
}

function ToastMessage({ item, onDismiss }: { item: ToastItem; onDismiss: (id: number) => void }) {
  const [paused, setPaused] = useState(false)
  const remaining = useRef(item.duration ?? DEFAULT_DURATION)
  const startedAt = useRef(0)
  const persistent = item.tone === 'error'
  useEffect(() => {
    if (persistent || paused) return
    startedAt.current = Date.now()
    const timer = window.setTimeout(() => onDismiss(item.id), Math.max(0, remaining.current))
    return () => {
      window.clearTimeout(timer)
      remaining.current -= Date.now() - startedAt.current
    }
  }, [item.id, onDismiss, paused, persistent])
  const isError = item.tone === 'error'
  return (
    <div
      className={cx('ui-toast', `tone-${item.tone}`)}
      role={isError ? 'alert' : 'status'}
      aria-live={isError ? 'assertive' : 'polite'}
      data-tone={item.tone}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setPaused(false) }}
    >
      <Icon name={isError ? 'alert' : item.tone === 'info' ? 'info' : 'check'} className="ui-toast-icon" />
      <span className="ui-toast-text">{item.text}</span>
      {item.action && (
        <button type="button" className="text-button ui-toast-action" onClick={() => { item.action?.onClick(); onDismiss(item.id) }}>
          {item.action.label}
        </button>
      )}
      <button type="button" className="icon-button ui-icon-button ui-toast-close" onClick={() => onDismiss(item.id)} aria-label={UI_TEXT.closeMessage}>
        <Icon name="x" size="sm" />
      </button>
    </div>
  )
}
