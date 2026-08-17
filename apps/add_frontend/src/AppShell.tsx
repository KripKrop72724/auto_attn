import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { Icon } from './Icon'
import type { DashboardRoute } from './types'
import type { RealtimeState } from './realtime'

const navigation = [
  { id: 'fleet', label: 'Fleet', icon: 'grid' },
  { id: 'users', label: 'Users', icon: 'users' },
  { id: 'attendance', label: 'Attendance', icon: 'clock' },
  { id: 'reconciliation', label: 'Reconciliation', icon: 'refresh' },
  { id: 'firmware', label: 'Firmware', icon: 'terminal' },
  { id: 'alerts', label: 'Alerts', icon: 'alert' },
] as const

const mobilePrimary = new Set<DashboardRoute>(['fleet', 'users', 'attendance', 'alerts'])

export function AppShell({
  children,
  username,
  route,
  openAlertCount,
  onNavigate,
  onLogout,
  realtimeState,
  lastSyncAt,
  workspaceRef,
}: {
  children: ReactNode
  username: string
  route: DashboardRoute
  openAlertCount: number
  onNavigate: (route: DashboardRoute) => void
  onLogout: () => void
  realtimeState: RealtimeState
  lastSyncAt: Date | null
  workspaceRef: RefObject<HTMLElement | null>
}) {
  const [moreOpen, setMoreOpen] = useState(false)
  const moreTriggerRef = useRef<HTMLButtonElement>(null)
  const moreSheetRef = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!moreOpen) return
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const focusableSelector = 'button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex="-1"])'
    const focusable = () => Array.from(moreSheetRef.current?.querySelectorAll<HTMLElement>(focusableSelector) || [])
    focusable()[0]?.focus()
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        setMoreOpen(false)
        return
      }
      if (event.key !== 'Tab') return
      const controls = focusable()
      if (!controls.length) return
      const first = controls[0]
      const last = controls[controls.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      ;(previousFocus || moreTriggerRef.current)?.focus()
    }
  }, [moreOpen])
  const connectionLabel = {
    connecting: 'Connecting',
    live: 'Live sync',
    reconnecting: 'Reconnecting',
    stale: 'Cached data',
  }[realtimeState]
  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <a
          className="app-brand"
          href="/fleet"
          onClick={(event) => {
            event.preventDefault()
            onNavigate('fleet')
          }}
        >
          <img src="/state-life-logo.png" alt="State Life Insurance Corporation" />
          <span><strong>Attendance Device Dashboard</strong><small>National operations</small></span>
        </a>
        <nav aria-label="Primary navigation">
          {navigation.map((item) => (
            <button
              key={item.id}
              className={`${route === item.id ? 'active' : ''} ${mobilePrimary.has(item.id) ? 'mobile-primary' : 'mobile-secondary'}`}
              aria-current={route === item.id ? 'page' : undefined}
              onClick={() => onNavigate(item.id)}
            >
              <Icon name={item.icon} />
              <span>{item.label}</span>
              {item.id === 'alerts' && openAlertCount > 0 && (
                <span className="nav-count" aria-label={`${openAlertCount} open alerts`}>{openAlertCount}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-system">
          <span className={`live-sync connection-${realtimeState}`}><i /> {connectionLabel}</span>
          <small>{lastSyncAt ? `Last sync ${lastSyncAt.toLocaleTimeString('en-PK', { timeZone: 'Asia/Karachi' })} PKT` : 'Encrypted · audited · PKT'}</small>
        </div>
        <button ref={moreTriggerRef} className={`mobile-more-trigger ${moreOpen ? 'active' : ''}`} onClick={() => setMoreOpen(true)} aria-haspopup="dialog" aria-expanded={moreOpen}><Icon name="grid" /><span>More</span></button>
      </aside>
      <section ref={workspaceRef} className="app-workspace">
        <header className="app-header">
          <div className="mobile-brand">
            <img src="/state-life-logo.png" alt="" />
            <span><strong>ADD Command Center</strong><small>{route}</small></span>
          </div>
          <div className="operator-area">
            <span className={`live-sync connection-${realtimeState}`} title={lastSyncAt ? `Last successful sync ${lastSyncAt.toLocaleString('en-PK', { timeZone: 'Asia/Karachi' })} PKT` : 'Connecting to live operations'}><i /> {connectionLabel}</span>
            <span><strong>{username}</strong><small>State Life operator</small></span>
            <button className="icon-button" onClick={onLogout} aria-label="Sign out"><Icon name="logout" /></button>
          </div>
        </header>
        <main className="page-content">{children}</main>
      </section>
      {moreOpen && <div className="mobile-more-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setMoreOpen(false) }}>
        <section ref={moreSheetRef} className="mobile-more-sheet" role="dialog" aria-modal="true" aria-labelledby="mobile-more-title">
          <header><div><p className="eyebrow">ALL WORKSPACES</p><h2 id="mobile-more-title">More operations</h2></div><button className="icon-button" aria-label="Close more navigation" onClick={() => setMoreOpen(false)}><Icon name="x" /></button></header>
          {navigation.filter((item) => !mobilePrimary.has(item.id)).map((item) => <button key={item.id} className={route === item.id ? 'active' : ''} onClick={() => { onNavigate(item.id); setMoreOpen(false) }}><Icon name={item.icon} /><span><strong>{item.label}</strong><small>{item.id === 'reconciliation' ? 'Historical truth, recovery, and immutable evidence' : 'Signed releases, scope previews, and campaigns'}</small></span><Icon name="chevron" /></button>)}
        </section>
      </div>}
    </div>
  )
}
