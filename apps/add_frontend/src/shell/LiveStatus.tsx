// The Live indicator in the header and the "not live" banner under it
// (01-foundation C). Driven by the live stream state plus background refresh
// failures; background failures never pop up a message.
import { useEffect, useRef, useState } from 'react'
import { Icon } from '../Icon'
import { clock } from '../lib/time'
import type { RealtimeState } from '../realtime'
import { useDismiss } from '../ui/useDismiss'
import { LIVE_WORDS, REFRESH_WORDS } from '../words/home'

export type ConnectionDisplay = 'connecting' | 'live' | 'reconnecting' | 'offline'

export interface ConnectionHealth {
  display: ConnectionDisplay
  /** When the page stopped being live (or started connecting). */
  since: Date | null
  /** The banner under the header is showing. */
  banner: boolean
  /** "Back online" (3 s) or "Up to date" (2 s) after a recovery. */
  recovered: string
}

const CONNECTING_GRACE_MS = 10_000
const RECONNECTING_GRACE_MS = 30_000

/**
 * One answer for "is this page live": the stream state, plus a failed
 * background refresh (which already retried twice) counting as offline.
 */
export function useConnectionHealth(realtime: RealtimeState, lastSyncAt: Date | null, fetchFailed: boolean): ConnectionHealth {
  const display: ConnectionDisplay = realtime === 'stale' || fetchFailed ? 'offline' : realtime
  const [notLiveSince, setNotLiveSince] = useState<Date | null>(display === 'live' ? null : new Date())
  const [tick, setTick] = useState(0)
  const [recovered, setRecovered] = useState('')
  const previous = useRef<ConnectionDisplay>(display)

  useEffect(() => {
    const before = previous.current
    previous.current = display
    if (display === 'live') {
      setNotLiveSince(null)
      if (before === 'offline' || before === 'reconnecting') {
        setRecovered(fetchFailed ? '' : before === 'offline' ? REFRESH_WORDS.upToDate : LIVE_WORDS.backOnline)
        const timer = window.setTimeout(() => setRecovered(''), before === 'offline' ? 2_000 : 3_000)
        return () => window.clearTimeout(timer)
      }
      return
    }
    setRecovered('')
    setNotLiveSince((current) => current ?? new Date())
  }, [display, fetchFailed])

  // Re-check the grace periods while not live.
  useEffect(() => {
    if (display === 'live' || display === 'offline') return
    const timer = window.setInterval(() => setTick((value) => value + 1), 2_000)
    return () => window.clearInterval(timer)
  }, [display])

  void tick
  const waited = notLiveSince ? Date.now() - notLiveSince.getTime() : 0
  const banner = display === 'offline'
    || (display === 'connecting' && waited >= CONNECTING_GRACE_MS)
    || (display === 'reconnecting' && waited >= RECONNECTING_GRACE_MS)
  return { display, since: display === 'live' ? null : (lastSyncAt || notLiveSince), banner, recovered }
}

const chipWords = (display: ConnectionDisplay, phone: boolean) =>
  display === 'live' ? LIVE_WORDS.live
    : display === 'connecting' ? LIVE_WORDS.connecting
      : display === 'reconnecting' ? LIVE_WORDS.reconnecting
        : phone ? LIVE_WORDS.offlineShort : LIVE_WORDS.offline

const stateClass: Record<ConnectionDisplay, string> = {
  connecting: 'connection-connecting',
  live: 'connection-live',
  reconnecting: 'connection-reconnecting',
  offline: 'connection-stale',
}

/** The chip; a tap opens a short explanation with "Try now". */
export function LiveStatus({ health, lastSyncAt, phone, onTryNow }: {
  health: ConnectionHealth
  lastSyncAt: Date | null
  phone: boolean
  onTryNow: () => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  useDismiss(() => { setOpen(false); buttonRef.current?.focus() }, { enabled: open })
  useEffect(() => {
    if (!open) return
    const close = (event: PointerEvent) => {
      const target = event.target as Node
      if (panelRef.current?.contains(target) || buttonRef.current?.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [open])
  const words = chipWords(health.display, phone)
  const time = health.display === 'live' ? clock(lastSyncAt) : clock(health.since)
  return (
    <span className="live-status">
      <button
        ref={buttonRef}
        type="button"
        className={`live-sync ${stateClass[health.display]}`}
        aria-expanded={open}
        aria-controls="live-status-detail"
        onClick={() => setOpen((value) => !value)}
      >
        <i aria-hidden="true" /><span>{words}</span>
      </button>
      <span className="sr-only" role="status">{health.recovered}</span>
      {open && (
        <div ref={panelRef} id="live-status-detail" className="live-status-panel" role="dialog" aria-label={LIVE_WORDS.statusLabel}>
          <p>{health.display === 'live' ? LIVE_WORDS.liveDetail(time) : LIVE_WORDS.notLiveDetail(time)}</p>
          {health.display !== 'live' && <TryNow onTryNow={onTryNow} />}
        </div>
      )}
    </span>
  )
}

function TryNow({ onTryNow }: { onTryNow: () => Promise<void> }) {
  const [busy, setBusy] = useState(false)
  return (
    <button type="button" className="button secondary small" aria-disabled={busy || undefined} aria-busy={busy || undefined} onClick={() => { if (busy) return; setBusy(true); void onTryNow().finally(() => setBusy(false)) }}>
      {busy ? LIVE_WORDS.trying : LIVE_WORDS.tryNow}
    </button>
  )
}

/** The slim amber bar under the header while the page is not live. */
export function LiveBanner({ health, onTryNow }: { health: ConnectionHealth; onTryNow: () => Promise<void> }) {
  const text = health.banner ? LIVE_WORDS.banner(clock(health.since)) : health.recovered || ''
  // The status region stays mounted, so screen readers hear each change
  // (a live region inserted together with its text is often not read).
  const region = <div className="sr-only" role="status" aria-live="polite">{text}</div>
  if (!health.banner && !health.recovered) return region
  if (!health.banner) {
    return <>{region}<div className="live-banner is-recovered"><Icon name="check" size="sm" /><span>{health.recovered}</span></div></>
  }
  return (
    <>
      {region}
      <div className="live-banner">
        <Icon name="wifi" size="sm" />
        <span>{text}</span>
        <TryNow onTryNow={onTryNow} />
      </div>
    </>
  )
}
