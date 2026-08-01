import type { ReactNode } from 'react'
import { Icon } from './Icon'
import type { DashboardRoute } from './types'

const navigation = [
  { id: 'fleet', label: 'Fleet', icon: 'grid' },
  { id: 'users', label: 'Users', icon: 'users' },
  { id: 'attendance', label: 'Attendance', icon: 'clock' },
  { id: 'firmware', label: 'Firmware', icon: 'terminal' },
  { id: 'alerts', label: 'Alerts', icon: 'alert' },
] as const

export function AppShell({
  children,
  username,
  route,
  openAlertCount,
  onNavigate,
  onLogout,
}: {
  children: ReactNode
  username: string
  route: DashboardRoute
  openAlertCount: number
  onNavigate: (route: DashboardRoute) => void
  onLogout: () => void
}) {
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
              className={route === item.id ? 'active' : ''}
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
          <span className="live-sync"><i /> Live operations feed</span>
          <small>Encrypted · audited · PKT</small>
        </div>
      </aside>
      <section className="app-workspace">
        <header className="app-header">
          <div className="mobile-brand">
            <img src="/state-life-logo.png" alt="" />
            <span><strong>ADD Command Center</strong><small>{route}</small></span>
          </div>
          <div className="operator-area">
            <span className="live-sync"><i /> Live sync</span>
            <span><strong>{username}</strong><small>State Life operator</small></span>
            <button className="icon-button" onClick={onLogout} aria-label="Sign out"><Icon name="logout" /></button>
          </div>
        </header>
        <main className="page-content">{children}</main>
      </section>
    </div>
  )
}
