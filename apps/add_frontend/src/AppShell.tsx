// The app frame (01-foundation A-E, FND-9): the menu (sidebar ≥1024 px, a
// labelled rail 761-1023 px, a bottom bar on phones), the opaque header with
// the Division switch, the Live indicator and the account menu, the "not live"
// banner, the skip link, and focus on the page title after every page change.
import { useEffect, useRef, useState, type MouseEvent, type ReactNode, type RefObject } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation, useNavigationType } from 'react-router-dom'
import { api } from './api'
import { queryClient } from './data'
import { Icon, type IconName } from './Icon'
import { routesWithoutDivision, useDivision } from './lib/useDivision'
import type { RealtimeState } from './realtime'
import { routePath } from './routing'
import { AccountMenu, SignOutButton, TechnicalSwitch, operatorInitials } from './shell/AccountMenu'
import { LiveBanner, LiveStatus, useConnectionHealth } from './shell/LiveStatus'
import type { DashboardRoute } from './types'
import { capCount } from './ui/Badge'
import { DivisionScopeChip, DivisionSwitch } from './ui/DivisionSwitch'
import { Sheet } from './ui/Sheet'
import { useIsPhone } from './ui/useMediaQuery'
import { FRAME_WORDS, MENU_WORDS } from './words/home'
import './shell/shell.css'

export { operatorInitials }

type MenuItem = { id: DashboardRoute; icon: IconName }

const everyday: MenuItem[] = [
  { id: 'home', icon: 'home' },
  { id: 'devices', icon: 'grid' },
  { id: 'attendance', icon: 'clock' },
  { id: 'problems', icon: 'alert' },
  { id: 'people', icon: 'users' },
]
const advanced: MenuItem[] = [
  { id: 'recover', icon: 'refresh' },
  { id: 'updates', icon: 'update' },
  { id: 'setup', icon: 'tool' },
  { id: 'settings', icon: 'settings' },
]
/** Phone bottom bar: four everyday pages and More (People and Advanced tools live in More). */
const phoneBar = new Set<DashboardRoute>(['home', 'devices', 'attendance', 'problems'])

/** Set up a device is switched off in production until the new-box software ships (D-17/18). */
function useSetupSwitchedOff() {
  const capabilities = useQuery({
    queryKey: ['provisioning-capabilities', 'menu'],
    queryFn: ({ signal }) => api<{ enabled?: boolean }>('/api/v1/provisioning/capabilities', { signal }),
    staleTime: 5 * 60_000,
    retry: false,
  }, queryClient)
  return capabilities.data ? capabilities.data.enabled === false : false
}

function ProblemsBadge({ count }: { count: number | null }) {
  if (count === null || count <= 0) return null
  return (
    <span className="nav-count">
      <span aria-hidden="true">{capCount(count, 99)}</span>
      <span className="sr-only">{FRAME_WORDS.problemsBadge(count)}</span>
    </span>
  )
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
  fetchFailed,
  onTryNow,
  workspaceRef,
}: {
  children: ReactNode
  username: string
  route: DashboardRoute
  /** Null until the first overview: the badge stays hidden instead of showing 0. */
  openAlertCount: number | null
  onNavigate: (route: DashboardRoute) => void
  onLogout: () => Promise<void>
  realtimeState: RealtimeState
  lastSyncAt: Date | null
  /** A background refresh failed (after its retries): the page shows what it knew. */
  fetchFailed?: boolean
  /** "Try now": re-read the devices and the overview. */
  onTryNow?: () => Promise<void>
  workspaceRef: RefObject<HTMLElement | null>
}) {
  const location = useLocation()
  const navigationType = useNavigationType()
  const phone = useIsPhone()
  const [division, setDivision] = useDivision()
  const [moreOpen, setMoreOpen] = useState(false)
  const [scrolled, setScrolled] = useState(false)
  const moreTriggerRef = useRef<HTMLButtonElement>(null)
  const setupOff = useSetupSwitchedOff()
  const health = useConnectionHealth(realtimeState, lastSyncAt, Boolean(fetchFailed))
  const tryNow = onTryNow || (async () => undefined)
  const showDivision = !routesWithoutDivision(location.pathname) || /^\/devices\/[^/]+/.test(location.pathname)

  useEffect(() => {
    const workspace = workspaceRef.current
    if (!workspace) return
    const update = () => setScrolled(workspace.scrollTop > 56)
    update()
    workspace.addEventListener('scroll', update, { passive: true })
    return () => workspace.removeEventListener('scroll', update)
  }, [workspaceRef])

  // The More sheet belongs to phones: close it when the window widens.
  useEffect(() => {
    if (!phone && moreOpen) setMoreOpen(false)
  }, [moreOpen, phone])

  // After an in-app page change (not the first load, not Back/Forward), move
  // focus to the new page's title so keyboard and screen-reader users start there.
  const firstRoute = useRef(true)
  useEffect(() => {
    if (firstRoute.current) {
      firstRoute.current = false
      return
    }
    if (navigationType === 'POP') return
    let tries = 0
    // Pages load their code lazily: look for the title for up to 5 s.
    const timer = window.setInterval(() => {
      const heading = workspaceRef.current?.querySelector<HTMLElement>('.page-content h1')
      if (heading) {
        if (!heading.hasAttribute('tabindex')) heading.setAttribute('tabindex', '-1')
        heading.focus({ preventScroll: true })
      }
      if (heading || ++tries > 100) window.clearInterval(timer)
    }, 50)
    return () => window.clearInterval(timer)
    // Only a change of page counts; filters (?status=…) and the device panel do not.
  }, [route]) // eslint-disable-line react-hooks/exhaustive-deps

  const go = (id: DashboardRoute) => (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return
    event.preventDefault()
    setMoreOpen(false)
    onNavigate(id)
  }
  const href = (id: DashboardRoute) => routePath(id, undefined, { division })
  const label = (id: DashboardRoute) => MENU_WORDS[id].label
  const current = MENU_WORDS[route]
  const moreActive = !phoneBar.has(route)

  const menuLink = (item: MenuItem, extra = '') => (
    <a
      key={item.id}
      href={href(item.id)}
      className={`nav-link ${route === item.id ? 'active' : ''} ${extra}`}
      aria-current={route === item.id ? 'page' : undefined}
      onClick={go(item.id)}
    >
      <Icon name={item.icon} />
      <span className="nav-label">{label(item.id)}</span>
      {item.id === 'problems' && <ProblemsBadge count={openAlertCount} />}
      {item.id === 'setup' && setupOff && <span className="nav-off">{FRAME_WORDS.switchedOff}</span>}
    </a>
  )

  const moreLink = (item: MenuItem) => (
    <a
      key={item.id}
      href={href(item.id)}
      className={`more-link ${route === item.id ? 'active' : ''}`}
      aria-current={route === item.id ? 'page' : undefined}
      onClick={go(item.id)}
    >
      <Icon name={item.icon} />
      <span>
        <strong>{label(item.id)}{item.id === 'setup' && setupOff && <span className="nav-off">{FRAME_WORDS.switchedOff}</span>}</strong>
        <small>{MENU_WORDS[item.id].description}</small>
      </span>
      <Icon name="chevron" size="sm" />
    </a>
  )

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main" onClick={(event) => { event.preventDefault(); document.getElementById('main')?.focus() }}>{FRAME_WORDS.skipLink}</a>
      <aside className="app-sidebar">
        <a className="app-brand" href={href('home')} onClick={go('home')}>
          <img src="/state-life-logo.png" alt="State Life Insurance Corporation" />
          <span><strong>{FRAME_WORDS.brandName}</strong><small>{FRAME_WORDS.brandSub}</small></span>
        </a>
        <nav aria-label={FRAME_WORDS.mainMenu}>
          <div className="nav-group">
            {everyday.map((item) => menuLink(item, phoneBar.has(item.id) ? 'mobile-primary' : 'mobile-secondary'))}
          </div>
          <div className="nav-group nav-advanced" role="group" aria-labelledby="nav-advanced-title">
            <p id="nav-advanced-title" className="nav-heading">{FRAME_WORDS.advancedTools}</p>
            {advanced.map((item) => menuLink(item, 'mobile-secondary'))}
          </div>
          <button
            ref={moreTriggerRef}
            type="button"
            className={`mobile-more-trigger ${moreOpen || moreActive ? 'active' : ''}`}
            onClick={() => setMoreOpen(true)}
            aria-haspopup="dialog"
            aria-expanded={moreOpen}
          >
            <Icon name="more" /><span>{FRAME_WORDS.more}</span>
          </button>
        </nav>
        <div className="sidebar-footer">
          <span>{FRAME_WORDS.pakistanTime}</span>
          <a href="/status" target="_blank" rel="noopener">{FRAME_WORDS.publicStatus} <Icon name="external" size="sm" /><span className="sr-only"> {FRAME_WORDS.opensInNewTab}</span></a>
        </div>
      </aside>
      <section ref={workspaceRef} className="app-workspace">
        <header className={`app-header ${scrolled ? 'is-scrolled' : ''}`}>
          <div className="app-header-start">
            <img className="app-header-logo" src="/state-life-logo.png" alt="" />
            {showDivision && !phone && <DivisionSwitch value={division} onChange={setDivision} className="header-division" />}
            <strong className="app-header-title" aria-hidden={!scrolled || undefined}>{current.label}</strong>
          </div>
          <div className="operator-area">
            {showDivision && phone && division !== 'all' && (
              <button type="button" className="header-division-chip" onClick={() => workspaceRef.current?.scrollTo({ top: 0, behavior: 'smooth' })}>
                {division === 'life' ? 'Life' : 'Health'}
              </button>
            )}
            <LiveStatus health={health} lastSyncAt={lastSyncAt} phone={phone} onTryNow={tryNow} />
            {!phone && <AccountMenu username={username} onLogout={onLogout} onSettings={go('settings')} />}
          </div>
        </header>
        <LiveBanner health={health} onTryNow={tryNow} />
        <main id="main" tabIndex={-1} className="page-content">
          {showDivision && phone && (
            <div className="phone-division-row">
              <DivisionSwitch value={division} onChange={setDivision} fullWidth />
            </div>
          )}
          {showDivision && <DivisionScopeChip division={division} onShowAll={() => setDivision('all')} className="frame-division-chip" />}
          <div className="page-enter" key={route}>{children}</div>
        </main>
      </section>
      {moreOpen && (
        <Sheet
          title={FRAME_WORDS.moreTitle}
          onClose={() => setMoreOpen(false)}
          returnFocusRef={moreTriggerRef}
          placement="bottom"
          className="more-sheet"
        >
          <div className="more-account">
            <span className="operator-avatar" aria-hidden="true" data-user-text>{operatorInitials(username)}</span>
            <span className="more-who">{FRAME_WORDS.signedInAs(username)}</span>
            <SignOutButton onLogout={onLogout} className="button secondary" />
          </div>
          <nav className="more-links" aria-label={FRAME_WORDS.more}>
            {moreLink(everyday[4])}
            <p className="nav-heading">{FRAME_WORDS.advancedTools}</p>
            {advanced.map(moreLink)}
          </nav>
          <div className="more-footer">
            <TechnicalSwitch id="more-technical" />
            <p>{FRAME_WORDS.pakistanTime}</p>
            <a href="/status" target="_blank" rel="noopener">{FRAME_WORDS.publicStatus} <Icon name="external" size="sm" /><span className="sr-only"> {FRAME_WORDS.opensInNewTab}</span></a>
          </div>
        </Sheet>
      )}
    </div>
  )
}
