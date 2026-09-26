import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { Icon } from './Icon'
import type { DashboardRoute } from './types'
import type { RealtimeState } from './realtime'

const navigation = [
  { id: 'fleet', label: 'Fleet', icon: 'grid', description: 'Live device health across the national fleet' },
  { id: 'users', label: 'Users', icon: 'users', description: 'Terminal identities for one selected device' },
  { id: 'attendance', label: 'Attendance', icon: 'clock', description: 'Punches, Oracle delivery, and release reviews' },
  { id: 'reconciliation', label: 'Reconciliation', icon: 'refresh', description: 'Terminal history, recovery, and source evidence' },
  { id: 'firmware', label: 'Firmware', icon: 'terminal', description: 'Signed releases, device preparation, and campaigns' },
  { id: 'alerts', label: 'Alerts', icon: 'alert', description: 'Open device conditions and their history' },
] as const

const mobilePrimary = new Set<DashboardRoute>(['fleet', 'users', 'attendance', 'alerts'])

const connectionLabels: Record<RealtimeState, string> = {
  connecting: 'Connecting',
  live: 'Live sync',
  reconnecting: 'Reconnecting',
  stale: 'Cached data',
}

const pktTime = (value: Date) => value.toLocaleTimeString('en-PK', { timeZone: 'Asia/Karachi', hour: 'numeric', minute: '2-digit', second: '2-digit' })

export function operatorInitials(name: string) {
  const words = name.replace(/([a-z])([A-Z])/g, '$1 $2').split(/[\s._@-]+/).filter(Boolean)
  if (!words.length) return '?'
  const letters = words.length === 1 ? words[0].slice(0, 2) : `${words[0][0]}${words[words.length - 1][0]}`
  return letters.toUpperCase()
}

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
  const [scrolled, setScrolled] = useState(false)
  const moreTriggerRef = useRef<HTMLButtonElement>(null)
  const moreSheetRef = useRef<HTMLElement>(null)
  useEffect(() => {
    const workspace = workspaceRef.current
    if (!workspace) return
    const update = () => setScrolled(workspace.scrollTop > 56)
    update()
    workspace.addEventListener('scroll', update, { passive: true })
    return () => workspace.removeEventListener('scroll', update)
  }, [workspaceRef])
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
  const connectionLabel = connectionLabels[realtimeState]
  const current = navigation.find((item) => item.id === route) || navigation[0]
  const secondaryActive = !mobilePrimary.has(route)
  const syncDetail = lastSyncAt ? `Last successful sync ${pktTime(lastSyncAt)} PKT` : 'Connecting to live operations'
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
          <span><strong>State Life</strong><small>Attendance devices</small></span>
        </a>
        <nav aria-label="Primary navigation">
          {navigation.map((item) => (
            <button
              key={item.id}
              className={`${route === item.id ? 'active' : ''} ${mobilePrimary.has(item.id) ? 'mobile-primary' : 'mobile-secondary'}`}
              aria-current={route === item.id ? 'page' : undefined}
              title={item.label}
              onClick={() => onNavigate(item.id)}
            >
              <Icon name={item.icon} />
              <span>{item.label}</span>
              {item.id === 'alerts' && openAlertCount > 0 && (
                <span className="nav-count" aria-label={`${openAlertCount} open alerts`}>{openAlertCount > 99 ? '99+' : openAlertCount}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-footer"><strong>Pakistan Standard Time</strong><span>All times are shown in PKT</span></div>
        <button ref={moreTriggerRef} className={`mobile-more-trigger ${moreOpen || secondaryActive ? 'active' : ''}`} onClick={() => setMoreOpen(true)} aria-haspopup="dialog" aria-expanded={moreOpen}><Icon name="menu" /><span>More</span></button>
      </aside>
      <section ref={workspaceRef} className="app-workspace">
        <header className={`app-header ${scrolled ? 'is-scrolled' : ''}`}>
          <div className="app-header-context">
            <img src="/state-life-logo.png" alt="" />
            <strong>{current.label}</strong>
          </div>
          <div className="operator-area">
            <span className={`live-sync connection-${realtimeState}`} role="status" title={syncDetail}><i aria-hidden="true" /><span>{connectionLabel}</span></span>
            <span className="operator">
              <span className="operator-avatar" aria-hidden="true">{operatorInitials(username)}</span>
              <span><strong>{username}</strong><small>State Life operator</small></span>
            </span>
            <button className="icon-button" onClick={onLogout} aria-label="Sign out" title="Sign out"><Icon name="logout" /></button>
          </div>
        </header>
        <main className="page-content">{children}</main>
      </section>
      {moreOpen && <div className="mobile-more-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setMoreOpen(false) }}>
        <section ref={moreSheetRef} className="mobile-more-sheet" role="dialog" aria-modal="true" aria-labelledby="mobile-more-title">
          <header><h2 id="mobile-more-title">More operations</h2><button className="icon-button" aria-label="Close more navigation" onClick={() => setMoreOpen(false)}><Icon name="x" /></button></header>
          {navigation.filter((item) => !mobilePrimary.has(item.id)).map((item) => <button key={item.id} className={route === item.id ? 'active' : ''} aria-current={route === item.id ? 'page' : undefined} onClick={() => { onNavigate(item.id); setMoreOpen(false) }}><Icon name={item.icon} /><span><strong>{item.label}</strong><small>{item.description}</small></span><Icon name="chevron" /></button>)}
        </section>
      </div>}
    </div>
  )
}
