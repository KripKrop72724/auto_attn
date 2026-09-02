import {
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  ReactNode,
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import { createBrowserRouter, RouterProvider, useLocation, useNavigate, useNavigationType, useRouteError } from 'react-router-dom'
import { api, ApiError, queryString, setCsrfToken } from './api'
import { queryClient } from './data'
import { AppShell } from './AppShell'
import { Icon } from './Icon'
import { dashboardRoute, firmwareSection, routeDeviceId, routePath } from './routing'
import { useRealtime, type RealtimeTopic } from './realtime'
import { normalizedStatus, statusPattern } from './status'
import type {
  Alert,
  AlertQueueResponse,
  AttendanceEvent,
  Command,
  ConnectionEvent,
  Device,
  DeviceLog,
  DeviceUser,
  FirmwareCampaign,
  FirmwareRelease,
  FirmwareScopePreview,
  HistoricalIdentityCandidate,
  HistoricalIdentityReport,
  IdentityConflictGroup,
  IdentityConflictReport,
  IdentityIntegrity,
  Overview,
  ReconciliationJob,
  ReconciliationDivergenceDetail,
  ReconciliationDivergenceReveal,
  ReconciliationPreflight,
  ReconciliationScheduler,
  SourceException,
  SourceExceptionList,
  SourceExceptionReveal,
  SourceExceptionTotals,
  UserCommandResponse,
  UserDeletionJob,
  DashboardRoute,
} from './types'

type View = DashboardRoute
export type DrawerTab = 'overview' | 'logs' | 'control'
type ToastState = { kind: 'notice' | 'error'; text: string } | null
export type UserDialogState =
  | { mode: 'create' }
  | { mode: 'edit'; user: DeviceUser }
  | { mode: 'delete'; user: DeviceUser }
  | { mode: 'lease'; user: DeviceUser }
  | null
export type IdentityResolutionDialogState = {
  mode: 'resolve' | 'revoke'
  group: IdentityConflictGroup
} | null
export type HistoricalIdentityDialogState = {
  candidate: HistoricalIdentityCandidate
} | null
export type ReconciliationDialogState =
  | { mode: 'start' }
  | { mode: 'control'; job: ReconciliationJob; action: 'pause' | 'resume' | 'cancel' | 'retry' }
  | null

export const terminalCommandStates = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'])
export const drawerTabs: DrawerTab[] = ['overview', 'logs', 'control']
export { statusPattern } from './status'

export const dateTime = (value?: string | null) =>
  value
    ? new Intl.DateTimeFormat('en-PK', {
        dateStyle: 'medium',
        timeStyle: 'medium',
        timeZone: 'Asia/Karachi',
      }).format(new Date(value))
    : '—'

export const relativeTime = (value?: string | null) => {
  if (!value) return 'Never seen'
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000)
  const formatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  if (Math.abs(seconds) < 60) return formatter.format(seconds, 'second')
  const minutes = Math.round(seconds / 60)
  if (Math.abs(minutes) < 60) return formatter.format(minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 48) return formatter.format(hours, 'hour')
  return formatter.format(Math.round(hours / 24), 'day')
}

export const pktInputToUtc = (value: string) => value ? new Date(`${value}:00+05:00`).toISOString() : ''

export const pktTodayBounds = (now = new Date()) => {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Karachi', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(now)
  const value = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value || ''
  const day = `${value('year')}-${value('month')}-${value('day')}`
  return { from: `${day}T00:00`, to: `${day}T23:59` }
}

export const idempotency = (prefix: string) => {
  const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
  return `${prefix}:${id}`
}

export const utf8Length = (value: string) => new TextEncoder().encode(value).length

const utf8Prefix = (value: string, maxBytes: number) => {
  let output = ''
  for (const character of value) {
    if (utf8Length(output + character) > maxBytes) break
    output += character
  }
  return output
}

export function identityConflictText(user: DeviceUser) {
  const matches = user.identity_conflict_members
    .map((member) => `${member.user_id} (UID ${member.uid})`)
    .join(', ')
  if (user.identity_conflict_resolved) {
    return matches
      ? `${user.cnic_masked || 'Masked CNIC'} · Verified same employee across user ${matches}`
      : `${user.cnic_masked || 'Masked CNIC'} · Verified same-employee terminal alias`
  }
  return matches
    ? `${user.cnic_masked || 'Masked CNIC'} · Exact CNIC also encoded on user ${matches} · correction required`
    : `${user.cnic_masked || 'Masked CNIC'} · Exact duplicate reported by terminal · correction required`
}

export function buildMachinePreview(
  displayName: string,
  cnic: string,
  shiftWorker: boolean,
  byteLimit = 24,
) {
  const canonical = displayName.trim().replace(/\s+/g, ' ')
  const suffix = `-${shiftWorker ? 'S-' : ''}${cnic}`
  return `${utf8Prefix(canonical, Math.max(0, byteLimit - utf8Length(suffix)))}${suffix}`
}

export function validateUserDraft(values: {
  displayName: string
  cnic: string
  password: string
  userIdOverride?: string
}) {
  if (!values.displayName.trim()) return 'Full name is required.'
  if (!/^\d{13}$/.test(values.cnic)) return 'CNIC must contain exactly 13 digits.'
  if (values.userIdOverride && !/^\d+$/.test(values.userIdOverride)) {
    return 'The optional employee/user ID must be numeric.'
  }
  if (!values.password) return 'Password confirmation is required.'
  return null
}

export const confirmationMatches = (value: string, user: DeviceUser) =>
  value === user.display_name || value === user.user_id

export const bulkDeletionConfirmation = (count: number, deviceId: string) =>
  `DELETE ${count} USERS FROM ${deviceId}`

export function StatusBadge({
  state,
  live = false,
}: {
  state?: string | null
  live?: boolean
}) {
  const status = normalizedStatus(state)
  const label = status.replaceAll('_', ' ')
  const pattern = statusPattern(status)
  const icon =
    pattern === 'confirmed'
      ? 'check'
      : pattern === 'blocked'
        ? 'alert'
        : pattern === 'waiting'
          ? 'pause'
          : 'info'
  return (
    <span className={`status-badge pattern-${pattern}`} data-pattern={pattern} aria-label={`Status: ${label}`} title={label}>
      <Icon name={icon} />
      <span>{label}</span>
      {live && <i aria-hidden="true" />}
    </span>
  )
}

export function useToast() {
  const [toast, setToast] = useState<ToastState>(null)
  useEffect(() => {
    if (!toast) return
    const timeout = window.setTimeout(() => setToast(null), 5000)
    return () => window.clearTimeout(timeout)
  }, [toast])
  const notice = useCallback((text: string) => setToast({ kind: 'notice', text }), [])
  const error = useCallback((text: string) => setToast({ kind: 'error', text }), [])
  return useMemo(() => ({ toast, notice, error }), [error, notice, toast])
}

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
    const node = panel.current
    const focusable = () =>
      Array.from(
        node?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
        ) || [],
      )
    const focusFrame = window.requestAnimationFrame(() => focusable()[0]?.focus())
    const handle = (event: KeyboardEvent) => {
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
            <p className="eyebrow">STATE LIFE · SECURE OPERATION</p>
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

const Login = lazy(() => import('./features/Login'))
const FleetMap = lazy(() => import('./features/FleetMap').then((module) => ({ default: module.FleetMap })))

export function PageHeader({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string
  title: string
  description: string
  action?: ReactNode
}) {
  return (
    <header className="page-header">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </header>
  )
}

export function Metric({ label, value, detail, icon, tone = 'neutral', onClick }: { label: string; value: string | number; detail: string; icon: Parameters<typeof Icon>[0]['name']; tone?: 'neutral' | 'positive' | 'warning' | 'critical'; onClick?: () => void }) {
  const content = <>
      <span className="metric-icon"><Icon name={icon} /></span>
      <div><p>{label}</p><strong>{value}</strong><small>{detail}</small></div>
    </>
  return onClick
    ? <button className={`metric-card metric-${tone} metric-link`} onClick={onClick} aria-label={`${label}: ${value}. ${detail}`}>{content}</button>
    : <article className={`metric-card metric-${tone}`}>{content}</article>
}

function FleetView({
  devices,
  overview,
  loading,
  onInspect,
  onManageUsers,
  onNavigateAlerts,
}: {
  devices: Device[]
  overview: Overview
  loading: boolean
  onInspect: (device: Device) => void
  onManageUsers: (device: Device) => void
  onNavigateAlerts: () => void
}) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('ALL')
  const [sort, setSort] = useState<'name' | 'last-contact' | 'state'>('last-contact')
  const [mode, setMode] = useState<'map' | 'list'>('map')
  const [inventory, setInventory] = useState<'fleet' | 'spares'>('fleet')
  const activeDevices = devices.filter((device) => !device.is_spare)
  const spareDevices = devices.filter((device) => device.is_spare)
  const inventoryDevices = inventory === 'fleet' ? activeDevices : spareDevices
  const shown = inventoryDevices.filter(
    (device) =>
      (inventory === 'spares' || filter === 'ALL' || device.state === filter) &&
      `${device.display_name} ${device.zone_name} ${device.hardware_id} ${device.zkt?.serial || ''}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  ).sort((left, right) => sort === 'name'
    ? left.display_name.localeCompare(right.display_name)
    : sort === 'state'
      ? left.state.localeCompare(right.state) || left.display_name.localeCompare(right.display_name)
      : +new Date(right.last_seen_at || 0) - +new Date(left.last_seen_at || 0))
  const online = overview.online || 0
  const availability = overview.total ? Math.round((online / overview.total) * 100) : 0
  const attention =
    (overview.offline || 0) +
    (overview.degraded || 0) +
    (overview.flapping || 0) +
    (overview.quarantined_duplicate_serial || 0)
  const delivery = overview.ords_delivery
  return (
    <>
      <PageHeader
        eyebrow={inventory === 'fleet' ? 'NATIONAL FLEET' : 'SPARE INVENTORY'}
        title={inventory === 'fleet' ? 'Attendance device command center' : 'Spare device inventory'}
        description={inventory === 'fleet'
          ? 'Live operational state of every assigned Zone Lite ESP and its ZKT terminal.'
          : 'Unassigned backup devices kept separate from active fleet health, outages, and operational issues.'}
        action={<div className="page-context"><span>{inventory === 'fleet' ? 'National footprint' : 'Ready reserve'}</span><strong>{inventory === 'fleet' ? `${overview.total} active pair${overview.total === 1 ? '' : 's'}` : `${overview.spares ?? spareDevices.length} spare device${(overview.spares ?? spareDevices.length) === 1 ? '' : 's'}`}</strong><small>{inventory === 'fleet' ? 'Live control · PKT' : 'Excluded from fleet health'}</small></div>}
      />
      <div className="section-tabs fleet-inventory-tabs" role="tablist" aria-label="Device inventory">
        <button role="tab" aria-selected={inventory === 'fleet'} className={inventory === 'fleet' ? 'active' : ''} onClick={() => setInventory('fleet')}><Icon name="grid" /> Active fleet <span>{activeDevices.length}</span></button>
        <button role="tab" aria-selected={inventory === 'spares'} className={inventory === 'spares' ? 'active' : ''} onClick={() => setInventory('spares')}><Icon name="server" /> Spares <span>{spareDevices.length}</span></button>
      </div>
      {inventory === 'fleet' ? <section className="metric-grid" aria-label="Fleet key indicators">
        <Metric label="Fleet availability" value={`${availability}%`} detail={`${online} of ${overview.total} connectors online`} icon="pulse" tone="positive" onClick={() => setFilter('ONLINE')} />
        <Metric label="Open operations queue" value={overview.open_alerts} detail={`${attention} device${attention === 1 ? '' : 's'} degraded or offline`} icon="alert" tone={overview.open_alerts ? 'warning' : 'positive'} onClick={onNavigateAlerts} />
        <Metric
          label="ORDS delivery queue"
          value={delivery?.backlog ?? 0}
          detail={`${delivery?.retrying ?? 0} retrying · ${delivery?.blocked_identity ?? 0} identity blocked · ${delivery?.quarantined ?? 0} quarantined`}
          icon="clock"
          tone={(delivery?.retrying ?? 0) > 0 ? 'critical' : (delivery?.backlog ?? 0) > 0 ? 'warning' : 'positive'}
        />
        <Metric label="Enrollment access" value={overview.active_leases} detail="Active temporary administrator leases" icon="shield" tone={overview.active_leases ? 'warning' : 'neutral'} />
      </section> : <section className="spare-inventory-banner" aria-label="Spare inventory monitoring policy">
        <span className="spare-inventory-icon"><Icon name="shield" /></span>
        <div><p className="eyebrow">MONITORING POLICY</p><h2>Ready when the active fleet needs a replacement</h2><p>Spare devices keep their telemetry and management access, but their offline state and alerts do not affect fleet availability or the operations queue.</p></div>
        <div className="spare-inventory-count"><strong>{spareDevices.length}</strong><span>Spare device{spareDevices.length === 1 ? '' : 's'}</span><small>Open a device to return it to active service.</small></div>
      </section>}
      <section className="panel">
        <header className="panel-header">
          <div><h2>{inventory === 'fleet' ? 'Live fleet' : 'Spare devices'}</h2><p>{inventory === 'fleet' ? 'State labels and border patterns remain readable without color.' : 'Reserve hardware is isolated from active operational reporting.'}</p></div>
          <div className="fleet-panel-actions">
            {inventory === 'fleet' && <div className="segmented-control fleet-view-toggle" role="group" aria-label="Fleet view">
              <button type="button" className={mode === 'map' ? 'active' : ''} aria-pressed={mode === 'map'} onClick={() => setMode('map')}><Icon name="map" /> Map</button>
              <button type="button" className={mode === 'list' ? 'active' : ''} aria-pressed={mode === 'list'} onClick={() => setMode('list')}><Icon name="list" /> List</button>
            </div>}
            <div className={`auto-onboard-note ${inventory === 'spares' ? 'spare-note' : ''}`}><Icon name="shield" /> {inventory === 'fleet' ? 'Secure auto-onboarding enabled' : 'Excluded from fleet health'}</div>
          </div>
        </header>
        <div className="toolbar">
          <label className="search-field">
            <span className="sr-only">{inventory === 'fleet' ? 'Search active fleet' : 'Search spare devices'}</span>
            <Icon name="search" />
            <input
              placeholder={inventory === 'fleet' ? 'Search active fleet' : 'Search spare devices'}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          {inventory === 'fleet' && <label>
            <span className="sr-only">Filter by state</span>
            <select value={filter} onChange={(event) => setFilter(event.target.value)}>
              <option value="ALL">All states</option>
              <option value="ONLINE">Online</option>
              <option value="DEGRADED">Degraded</option>
              <option value="FLAPPING">Flapping</option>
              <option value="OFFLINE">Offline</option>
              <option value="ONBOARDING">Onboarding</option>
              <option value="QUARANTINED_DUPLICATE_SERIAL">Quarantined</option>
            </select>
          </label>}
          <label><span className="sr-only">Sort fleet</span><select value={sort} onChange={(event) => setSort(event.target.value as typeof sort)}><option value="last-contact">Newest contact first</option><option value="name">Device name</option><option value="state">Operational state</option></select></label>
        </div>
        {inventory === 'fleet' && mode === 'map' ? (
          <Suspense fallback={<div className="fleet-map-fallback" role="status"><Icon name="refresh" /> Preparing national map…</div>}>
            <FleetMap
              devices={shown}
              loading={loading}
              onInspect={onInspect}
              onManageUsers={onManageUsers}
              formatRelativeTime={relativeTime}
            />
          </Suspense>
        ) : (
          <div className="device-list" aria-busy={loading}>
            {loading && <div className="empty-state"><Icon name="refresh" /><h3>Loading live fleet…</h3></div>}
            {!loading && shown.map((device) => (
              <article className={`device-card ${device.is_spare ? 'spare-device-card pattern-notice' : `pattern-${statusPattern(device.state)}`}`} key={device.connector_id}>
                <button className="device-card-main" onClick={() => onInspect(device)} aria-label={`Inspect ${device.display_name}`}>
                  <span className="device-symbol"><Icon name="server" /></span>
                  <span className="device-identity"><strong>{device.display_name}</strong><small>{device.zone_id} · {device.hardware_id}</small></span>
                  <span className="device-terminal"><strong>{device.zkt?.model || 'Awaiting terminal identity'}</strong><small>{device.zkt?.ip_address || 'No IP'} · {device.zkt?.serial || 'No serial'}</small></span>
                  <span className="device-activity"><strong>{device.is_spare ? 'Reserve inventory' : (device.current_activity || 'Idle')}</strong><small>{relativeTime(device.last_seen_at)}</small></span>
                  <StatusBadge state={device.is_spare ? 'SPARE' : device.state} live={!device.is_spare && device.connected} />
                  <Icon name="chevron" />
                </button>
                <div className="device-card-actions">
                  <button className="text-button" onClick={() => onManageUsers(device)}><Icon name="users" /> Manage users</button>
                  <span>{device.is_spare ? 'Excluded from fleet health and operational alerts · ' : ''}FW {device.firmware_version || 'unknown'} · {device.ota_capable ? (device.ota_state || 'OTA ready') : 'Manual firmware updates'} · {device.zkt?.certification_state || 'uncertified'}</span>
                </div>
              </article>
            ))}
            {!loading && !shown.length && (
              <div className="empty-state">
                <Icon name="server" />
                <h3>{inventoryDevices.length ? 'No devices match these filters.' : inventory === 'spares' ? 'No spare devices yet.' : 'Waiting for an authorized Zone Lite device to connect automatically.'}</h3>
                <p>{inventoryDevices.length ? 'Change the search or state filter.' : inventory === 'spares' ? 'Open any active device and move it to spare inventory when it is taken out of service.' : 'A securely flashed ESP will appear here after signed onboarding.'}</p>
              </div>
            )}
          </div>
        )}
      </section>
    </>
  )
}

export function CommandProgress({
  command,
  onCancel,
}: {
  command: Command
  onCancel: (command: Command) => Promise<void>
}) {
  const status = normalizedStatus(command.status)
  const commandType = normalizedStatus(command.type)
  const canCancel = !['RUNNING', 'CANCEL_REQUESTED', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'].includes(status)
  return (
    <section className={`command-progress pattern-${statusPattern(status)}`} aria-live="polite">
      <span className="command-symbol"><Icon name={status === 'SUCCEEDED' ? 'check' : status === 'FAILED' ? 'alert' : 'refresh'} /></span>
      <div>
        <p className="eyebrow">{commandType.replaceAll('_', ' ')}</p>
        <h3>{status.replaceAll('_', ' ')}</h3>
        <p>{command.error_message || command.error_code || `Command ${command.command_id.slice(0, 8)} is durably tracked.`}</p>
      </div>
      {canCancel && <button className="button secondary" onClick={() => void onCancel(command)}>Cancel before execution</button>}
    </section>
  )
}

const UsersView = lazy(() => import('./features/UsersWorkspace').then((module) => ({ default: module.UsersView })))

const ReconciliationView = lazy(() => import('./features/Reconciliation').then((module) => ({ default: module.ReconciliationView })))
const FirmwareView = lazy(() => import('./features/Firmware').then((module) => ({ default: module.FirmwareView })))
const FirmwareProvisioning = lazy(() => import('./features/FirmwareProvisioning'))

const AttendanceView = lazy(() => import('./features/Attendance').then((module) => ({ default: module.AttendanceView })))
const AlertsView = lazy(() => import('./features/Monitoring').then((module) => ({ default: module.AlertsView })))

export function formatAlertDiagnostics(details: Record<string, unknown>): string {
  const facts: string[] = []
  const category = details.failure_category
  if (typeof category === 'string' && /^[A-Z0-9_]{1,80}$/.test(category)) {
    facts.push(`Category ${category}`)
  }
  const httpStatus = details.http_status
  if (typeof httpStatus === 'number' && Number.isInteger(httpStatus) && httpStatus >= 100 && httpStatus <= 599) {
    facts.push(`HTTP ${httpStatus}`)
  }
  const attemptCount = details.attempt_count
  if (typeof attemptCount === 'number' && Number.isInteger(attemptCount) && attemptCount >= 0) {
    facts.push(`Attempt ${attemptCount}`)
  }
  const affectedUsers = details.affected_users
  if (typeof affectedUsers === 'number' && Number.isInteger(affectedUsers) && affectedUsers >= 0) {
    facts.push(`${affectedUsers} affected users`)
  }
  return facts.join(' · ')
}

const DeviceDrawer = lazy(() => import('./features/DeviceDrawer').then((module) => ({ default: module.DeviceDrawer })))

function DashboardApp() {
  const location = useLocation()
  const navigate = useNavigate()
  const navigationType = useNavigationType()
  const workspaceRef = useRef<HTMLElement>(null)
  const workspaceScrollPositions = useRef(new Map<string, number>())
  const previousScrollContext = useRef<{ key: string; context: string } | null>(null)
  const [authState, setAuthState] = useState<'loading' | 'anonymous' | 'authenticated'>('loading')
  const [username, setUsername] = useState('')
  const [devices, setDevices] = useState<Device[]>([])
  const [overview, setOverview] = useState<Overview>({ total: 0, open_alerts: 0, active_leases: 0 })
  const [loading, setLoading] = useState(true)
  const [revisions, setRevisions] = useState<Record<RealtimeTopic, number>>({
    attendance: 0, alert: 0, users: 0, reconciliation: 0, command: 0, log: 0,
    identity: 0, firmware: 0, provisioning: 0, device: 0, backend_error: 0, resync: 0,
  })
  const [selectedDeviceId, setSelectedDeviceId] = useState('')
  const [drawer, setDrawer] = useState<Device | null>(null)
  const toast = useToast()
  const view = dashboardRoute(location.pathname)
  const rememberWorkspaceScroll = useCallback(() => {
    const workspace = workspaceRef.current
    if (workspace) workspaceScrollPositions.current.set(location.key, workspace.scrollTop)
  }, [location.key])
  const setView = useCallback((next: View) => {
    rememberWorkspaceScroll()
    navigate(routePath(next))
  }, [navigate, rememberWorkspaceScroll])
  const scrollContext = useMemo(() => {
    if (view === 'fleet') return 'fleet'
    if (view === 'users') return `users:${routeDeviceId(location.pathname, 'users') || 'picker'}`
    if (view === 'firmware') return `firmware:${firmwareSection(location.search)}`
    if (view === 'reconciliation') {
      return `reconciliation:${new URLSearchParams(location.search).get('tab') || 'jobs'}`
    }
    return view
  }, [location.pathname, location.search, view])

  useLayoutEffect(() => {
    const workspace = workspaceRef.current
    if (!workspace) return
    const previous = previousScrollContext.current
    const sameFleetCanvas = previous?.context === 'fleet' && scrollContext === 'fleet'
    const contextChanged = previous !== null && previous.context !== scrollContext
    let target = workspace.scrollTop
    let shouldRestore = false
    if (!sameFleetCanvas && navigationType === 'POP') {
      target = workspaceScrollPositions.current.get(location.key) || 0
      shouldRestore = true
    } else if (!sameFleetCanvas && (previous === null || contextChanged)) {
      target = 0
      shouldRestore = true
    }

    let restorationComplete = !shouldRestore
    let resizeObserver: ResizeObserver | null = null
    const delayed: number[] = []
    const applyTarget = (finalAttempt = false) => {
      if (restorationComplete) return
      const maximum = Math.max(0, workspace.scrollHeight - workspace.clientHeight)
      workspace.scrollTop = Math.min(target, maximum)
      if (maximum >= target || finalAttempt) {
        restorationComplete = true
        resizeObserver?.disconnect()
      }
    }
    applyTarget()
    const frame = window.requestAnimationFrame(() => applyTarget())
    if (!restorationComplete) {
      ;[80, 250, 600].forEach((delay, index) => {
        delayed.push(window.setTimeout(() => applyTarget(index === 2), delay))
      })
    }
    const content = workspace.querySelector<HTMLElement>('.page-content')
    resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => applyTarget())
    if (!restorationComplete && content) resizeObserver?.observe(content)
    previousScrollContext.current = { key: location.key, context: scrollContext }

    const remember = () => workspaceScrollPositions.current.set(location.key, workspace.scrollTop)
    workspace.addEventListener('scroll', remember, { passive: true })
    return () => {
      window.cancelAnimationFrame(frame)
      delayed.forEach((timer) => window.clearTimeout(timer))
      resizeObserver?.disconnect()
      remember()
      workspace.removeEventListener('scroll', remember)
    }
  }, [location.key, navigationType, scrollContext])

  useEffect(() => {
    const timer = window.setInterval(() => setRevisions((current) => ({ ...current })), 60_000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    const legacy = window.location.hash.replace(/^#/, '') as View
    if (legacy && ['fleet', 'users', 'attendance', 'reconciliation', 'firmware', 'alerts'].includes(legacy)) {
      navigate(routePath(legacy), { replace: true })
      window.history.replaceState(null, '', routePath(legacy))
      return
    }
    if (location.pathname === '/' || dashboardRoute(location.pathname) !== location.pathname.split('/').filter(Boolean)[0]) {
      navigate('/fleet', { replace: true })
    }
  }, [location.pathname, navigate])

  useEffect(() => {
    const userDevice = routeDeviceId(location.pathname, 'users')
    if (userDevice && userDevice !== selectedDeviceId) setSelectedDeviceId(userDevice)
    else if (location.pathname === '/users' && selectedDeviceId) setSelectedDeviceId('')
  }, [location.pathname, selectedDeviceId])

  useEffect(() => {
    const fleetDevice = routeDeviceId(location.pathname, 'fleet')
    if (!fleetDevice) {
      if (drawer) setDrawer(null)
      return
    }
    const match = devices.find((device) => device.connector_id === fleetDevice)
    if (match && drawer?.connector_id !== match.connector_id) setDrawer(match)
  }, [devices, drawer, location.pathname])

  const refreshFleet = useCallback(async () => {
    setLoading(true)
    try {
      const [counts, fleet] = await Promise.all([
        queryClient.fetchQuery({ queryKey: ['overview'], queryFn: ({ signal }) => api<Overview>('/api/v1/overview', { signal }), staleTime: 0 }),
        queryClient.fetchQuery({ queryKey: ['devices'], queryFn: ({ signal }) => api<{ rows: Device[] }>('/api/v1/devices', { signal }), staleTime: 0 }),
      ])
      setOverview(counts)
      setDevices(fleet.rows)
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) setAuthState('anonymous')
      else toast.error(reason instanceof Error ? reason.message : 'Unable to refresh fleet.')
    } finally {
      setLoading(false)
    }
  }, [toast.error])

  const handleRealtimeTopics = useCallback((topics: ReadonlySet<RealtimeTopic>) => {
    setRevisions((current) => {
      const next = { ...current }
      topics.forEach((topic) => {
        next[topic] += 1
        if (topic === 'resync') {
          ;(['attendance', 'alert', 'users', 'reconciliation', 'command', 'log', 'identity', 'firmware', 'provisioning', 'device'] as RealtimeTopic[])
            .forEach((name) => { next[name] += 1 })
        }
      })
      return next
    })
    if (topics.has('device') || topics.has('resync') || topics.has('command')) void refreshFleet()
  }, [refreshFleet])
  const realtime = useRealtime(authState === 'authenticated', handleRealtimeTopics)

  useEffect(() => {
    api<{ username: string; csrf_token: string }>('/api/v1/auth/session')
      .then((session) => {
        setCsrfToken(session.csrf_token)
        setUsername(session.username)
        setAuthState('authenticated')
      })
      .catch(() => setAuthState('anonymous'))
  }, [])

  useEffect(() => {
    const handleSessionExpired = () => {
      setCsrfToken('')
      setAuthState('anonymous')
      setDevices([])
      queryClient.clear()
      setDrawer(null)
    }
    window.addEventListener('add:session-expired', handleSessionExpired)
    return () => window.removeEventListener('add:session-expired', handleSessionExpired)
  }, [])

  useEffect(() => {
    if (authState !== 'authenticated') return
    void refreshFleet()
  }, [authState, refreshFleet])

  const login = async (loginUsername: string, password: string) => {
    const response = await api<{ username: string; csrf_token: string }>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: loginUsername, password }),
    })
    setCsrfToken(response.csrf_token)
    setUsername(response.username)
    setAuthState('authenticated')
  }

  const logout = async () => {
    try { await api('/api/v1/auth/logout', { method: 'POST', body: '{}' }) } finally {
      setCsrfToken('')
      setAuthState('anonymous')
      setDevices([])
      queryClient.clear()
    }
  }

  const manageUsers = (device: Device) => {
    setSelectedDeviceId(device.connector_id)
    setDrawer(null)
    navigate(routePath('users', device.connector_id))
  }

  const inspectDevice = (device: Device) => {
    setDrawer(device)
    navigate(routePath('fleet', device.connector_id))
  }

  const closeDevice = () => {
    setDrawer(null)
    navigate('/fleet')
  }

  const selectUserDevice = (id: string) => {
    setSelectedDeviceId(id)
    navigate(id ? routePath('users', id) : '/users')
  }

  if (authState === 'loading') return <main className="boot-screen"><img src="/state-life-logo.png" alt="State Life Insurance Corporation" /><p>Opening Attendance Device Dashboard…</p></main>
  if (authState === 'anonymous') return <Suspense fallback={<main className="boot-screen"><img src="/state-life-logo.png" alt="State Life Insurance Corporation" /><p>Opening secure sign in…</p></main>}><Login onLogin={login} /></Suspense>

  return (
    <>
      <AppShell workspaceRef={workspaceRef} username={username} route={view} openAlertCount={overview.open_alerts} onNavigate={setView} onLogout={() => void logout()} realtimeState={realtime.state} lastSyncAt={realtime.lastSyncAt}>
        {view === 'fleet' && <FleetView devices={devices} overview={overview} loading={loading} onInspect={inspectDevice} onManageUsers={manageUsers} onNavigateAlerts={() => navigate('/alerts')} />}
        {view === 'users' && <Suspense fallback={<div className="panel empty-state">Opening selected-terminal users…</div>}><UsersView devices={devices} selectedDeviceId={selectedDeviceId} onSelectDevice={selectUserDevice} revision={revisions.users + revisions.identity + revisions.command} toast={toast} refreshFleet={refreshFleet} /></Suspense>}
        {view === 'attendance' && <Suspense fallback={<div className="panel empty-state">Opening immutable attendance ledger…</div>}><AttendanceView devices={devices} revision={revisions.attendance} realtimeState={realtime.state} realtimeLastSyncAt={realtime.lastSyncAt} /></Suspense>}
        {view === 'reconciliation' && <Suspense fallback={<div className="panel empty-state">Opening reconciliation workspace…</div>}><ReconciliationView devices={devices} revision={revisions.reconciliation + revisions.attendance} toast={toast} /></Suspense>}
        {view === 'firmware' && <Suspense fallback={<div className="panel empty-state">Opening firmware workspace…</div>}>{firmwareSection(location.search) === 'prepare' ? <FirmwareProvisioning revision={revisions.provisioning} toast={toast} username={username} onSection={(section) => navigate(`/firmware?tab=${section}`)} /> : <FirmwareView devices={devices} revision={revisions.firmware} toast={toast} section={firmwareSection(location.search)} onSection={(section) => navigate(`/firmware?tab=${section}`)} />}</Suspense>}
        {view === 'alerts' && <Suspense fallback={<div className="panel empty-state">Opening national alert queue…</div>}><AlertsView devices={devices.filter((device) => !device.is_spare)} toast={toast} revision={revisions.alert} /></Suspense>}
      </AppShell>
      {drawer && <Suspense fallback={null}><DeviceDrawer seed={drawer} revision={revisions.device + revisions.command + revisions.log} onClose={closeDevice} onManageUsers={manageUsers} onInventoryChanged={refreshFleet} toast={toast} /></Suspense>}
      {toast.toast && <div className={`toast pattern-${toast.toast.kind === 'error' ? 'blocked' : 'confirmed'}`} role={toast.toast.kind === 'error' ? 'alert' : 'status'} aria-live={toast.toast.kind === 'error' ? 'assertive' : 'polite'}><Icon name={toast.toast.kind === 'error' ? 'alert' : 'check'} />{toast.toast.text}</div>}
    </>
  )
}

function RouteErrorBoundary() {
  const error = useRouteError()
  return <main className="boot-screen route-error" role="alert"><img src="/state-life-logo.png" alt="State Life Insurance Corporation" /><h1>This workspace could not be opened.</h1><p>{error instanceof Error ? error.message : 'The requested route is unavailable.'}</p><a className="button primary" href="/fleet">Return to Fleet</a></main>
}

export default function App() {
  const router = useMemo(() => createBrowserRouter([{
    path: '*',
    element: <DashboardApp />,
    errorElement: <RouteErrorBoundary />,
  }]), [])
  return <RouterProvider router={router} />
}
