// The dashboard frame: sign-in, the live device list and overview, live
// updates, routes and redirects. Pages live in src/routes (one module each) and
// read what they need from AppData, so changing a page never needs this file.
import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { createBrowserRouter, RouterProvider, useLocation, useNavigate, useNavigationType } from 'react-router-dom'
import { api, ApiError, setCsrfToken } from './api'
import { AppDataProvider, type AppData, type Revisions } from './AppData'
import { queryClient } from './data'
import { deviceSnapshots, isFullDeviceRow, useDevices, useDevicesLoaded } from './deviceData'
import { AppShell } from './AppShell'
import { Icon } from './Icon'
import { parseDivision } from './lib/division'
import { useNewVersion } from './lib/newVersion'
import { useThrottledRunner } from './lib/throttle'
import { dashboardRoute, legacyRedirect, recoverSection, routeDeviceId, routePath, updatesSection } from './routing'
import { useRealtime, type RealtimeEvent, type RealtimeTopic } from './realtime'
import { documentTitle, routeTable } from './routeTable'
import type { DashboardRoute, Device, Overview } from './types'
import { NewVersionNotice } from './ui/NewVersionNotice'
import { PageErrorBoundary, RouteErrorBoundary } from './ui/PageError'
import { ToastProvider, useToast as useKitToast, type Toast } from './ui/Toast'
import { TechnicalDetailsProvider } from './ui/TechnicalDetailsContext'
import { usePrefs } from './lib/prefs'

// Moved modules, re-exported so the pages that import from '../App' keep working.
export * from './lib/dashboardTypes'
export * from './lib/idempotency'
export * from './lib/people'
export * from './lib/time'
export * from './ui/CommandProgress'
export * from './ui/Dialog'
export * from './ui/Metric'
export * from './ui/PageHeader'
export * from './ui/StatusBadge'
export * from './ui/StatusChip'
export * from './ui/Toast'
export * from './words/health'
export { statusPattern } from './status'

// The feature files type their toast prop as ReturnType<typeof useToast> from
// '../App', so this keeps the old return shape ({ toast, notice, error, dismiss })
// and their test doubles still fit. Import useToast from './ui' for show()/Undo.
export function useToast(): Toast {
  return useKitToast()
}

// Realtime refresh bounds. Device events with a whole row update the list in
// place; partial device events and command updates re-read it at most every
// 5 s. Alerts and the periodic resync refresh the heavier overview.
export const DEVICE_REFRESH_MS = 5_000
export const FLEET_OVERVIEW_REFRESH_MS = 15_000
export const BACKGROUND_OVERVIEW_REFRESH_MS = 60_000

/** Pages that show the full overview figures (delivery and recovery tiles). */
const FULL_OVERVIEW_ROUTES = new Set<DashboardRoute>(['home', 'devices'])
/** Topics whose events say which device they are for; others count for every open device. */
const DEVICE_TOPICS = new Set<RealtimeTopic>(['device', 'command', 'log', 'alert'])
const ALL_TOPICS: RealtimeTopic[] = [
  'attendance', 'alert', 'users', 'reconciliation', 'command', 'log',
  'identity', 'firmware', 'provisioning', 'device', 'backend_error', 'resync',
]

const Login = lazy(() => import('./features/Login'))
const DeviceDrawer = lazy(() => import('./features/DeviceDrawer').then((module) => ({ default: module.DeviceDrawer })))

const eventConnector = (event: RealtimeEvent): string => {
  const data = event.data
  if (!data || typeof data !== 'object' || Array.isArray(data)) return ''
  const id = (data as Record<string, unknown>).connector_id
  return typeof id === 'string' ? id : ''
}

function DashboardApp() {
  const location = useLocation()
  const navigate = useNavigate()
  const navigationType = useNavigationType()
  const workspaceRef = useRef<HTMLElement>(null)
  const workspaceScrollPositions = useRef(new Map<string, number>())
  const previousScrollContext = useRef<{ key: string; context: string } | null>(null)
  const [authState, setAuthState] = useState<'loading' | 'anonymous' | 'authenticated'>('loading')
  const [username, setUsername] = useState('')
  const devices = useDevices()
  const devicesLoaded = useDevicesLoaded()
  const deviceRequest = useRef(0)
  const overviewRequest = useRef(0)
  // Null until the first answer: nothing shows "0 of 0" before the data arrives.
  const [overview, setOverview] = useState<Overview | null>(null)
  const [devicesError, setDevicesError] = useState<unknown>(null)
  const [devicesUpdatedAt, setDevicesUpdatedAt] = useState<Date | null>(null)
  const [revisions, setRevisions] = useState<Revisions>(() => Object.fromEntries(ALL_TOPICS.map((topic) => [topic, 0])) as Revisions)
  // Per-device live revisions (DEV-4): an open device reloads for its own events only.
  const deviceRevisions = useRef(new Map<string, number>())
  const custodyRevisions = useRef(new Map<string, number>())
  const unscopedDeviceRevision = useRef(0)
  const deviceListNeeded = useRef(false)
  const [selectedDeviceId, setSelectedDeviceId] = useState('')
  const [drawer, setDrawer] = useState<Device | null>(null)
  const toast = useToast()
  const view = dashboardRoute(location.pathname)
  const division = parseDivision(location.search)
  // The device panel (/devices/:id) opens over the page it was opened from,
  // which stays mounted behind it (focus and scroll come back on close).
  const panelFrom = view === 'devices' && routeDeviceId(location.pathname, 'devices')
    ? (location.state as { from?: string } | null)?.from || ''
    : ''
  const pageRoute: DashboardRoute = panelFrom ? dashboardRoute(panelFrom.split(/[?#]/)[0]) : view
  const rememberWorkspaceScroll = useCallback(() => {
    const workspace = workspaceRef.current
    if (workspace) workspaceScrollPositions.current.set(location.key, workspace.scrollTop)
  }, [location.key])
  const setView = useCallback((next: DashboardRoute) => {
    rememberWorkspaceScroll()
    navigate(routePath(next, undefined, { division }))
  }, [division, navigate, rememberWorkspaceScroll])
  const scrollContext = useMemo(() => {
    // The device panel opens over the Devices list without moving it.
    if (panelFrom) return `panel:${pageRoute}`
    if (view === 'devices') return 'devices'
    if (view === 'people') return `people:${routeDeviceId(location.pathname, 'people') || 'picker'}`
    if (view === 'updates') return `updates:${updatesSection(location.search)}`
    if (view === 'recover') return `recover:${recoverSection(location.search)}`
    return view
  }, [location.pathname, location.search, pageRoute, panelFrom, view])

  useLayoutEffect(() => {
    const workspace = workspaceRef.current
    if (!workspace) return
    const previous = previousScrollContext.current
    // Opening or closing the device panel never moves the page behind it.
    const panelContext = (context?: string) => context?.startsWith('panel:') ? context.slice(6) : context
    const sameFleetCanvas = (previous?.context === 'devices' && scrollContext === 'devices')
      || (Boolean(previous) && previous?.context !== scrollContext && panelContext(previous?.context) === panelContext(scrollContext))
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

  // Old addresses (/fleet, /users/:id, /firmware?tab=prepare, #users, …) move to
  // the new pages with their query string and Division kept.
  useEffect(() => {
    const target = legacyRedirect(location.pathname, location.search, location.hash)
    if (target) navigate(target, { replace: true, state: location.state })
  }, [location.hash, location.pathname, location.search, location.state, navigate])

  useEffect(() => {
    document.title = documentTitle(pageRoute)
  }, [pageRoute])

  useEffect(() => {
    const userDevice = routeDeviceId(location.pathname, 'people')
    if (userDevice && userDevice !== selectedDeviceId) setSelectedDeviceId(userDevice)
    else if (location.pathname === '/people' && selectedDeviceId) setSelectedDeviceId('')
  }, [location.pathname, selectedDeviceId])

  useEffect(() => {
    const fleetDevice = routeDeviceId(location.pathname, 'devices')
    if (!fleetDevice) {
      if (drawer) setDrawer(null)
      return
    }
    const match = devices.find((device) => device.connector_id === fleetDevice)
    if (match && drawer?.connector_id !== match.connector_id) setDrawer(match)
  }, [devices, drawer, location.pathname])

  // Devices and overview refresh apart: heartbeats and commands only change the
  // device list, while the overview aggregates the whole attendance and ORDS
  // backlog and is far heavier on a busy server. Background refreshes never
  // touch the loading state, so a live page does not flicker, and a failed one
  // never pops up a message: the page says it is showing what it knew.
  const reportFleetError = useCallback((reason: unknown) => {
    if (reason instanceof ApiError && reason.status === 401) setAuthState('anonymous')
    else setDevicesError(reason || new Error('Unable to refresh fleet.'))
  }, [])

  const refreshDevices = useCallback(async () => {
    const request = ++deviceRequest.current
    try {
      // The slim list (PR-1a); an older backend ignores `view` and sends full rows.
      const fleet = await queryClient.fetchQuery({ queryKey: ['devices', 'list'], queryFn: ({ signal }) => api<{ rows: Device[] }>('/api/v1/devices?view=list', { signal }), staleTime: 0 })
      if (request !== deviceRequest.current) return
      deviceSnapshots.replaceFleet(fleet.rows)
      setDevicesError(null)
      setDevicesUpdatedAt(new Date())
    } catch (reason) {
      if (request === deviceRequest.current) reportFleetError(reason)
    }
  }, [reportFleetError])

  // Only Home and Devices show the delivery and recovery figures; elsewhere the
  // frame needs just the device and problem counts (`sections=fleet`, which
  // never waits for the heavy figures; an older backend sends the full answer).
  const viewRef = useRef(pageRoute)
  viewRef.current = pageRoute
  const refreshOverview = useCallback(async () => {
    const request = ++overviewRequest.current
    const full = FULL_OVERVIEW_ROUTES.has(viewRef.current)
    try {
      const counts = await queryClient.fetchQuery({
        queryKey: ['overview', full ? 'full' : 'fleet'],
        queryFn: ({ signal }) => api<Overview>(full ? '/api/v1/overview' : '/api/v1/overview?sections=fleet', { signal }),
        staleTime: 0,
      })
      if (request !== overviewRequest.current) return
      // A quick answer never wipes the delivery figures a full one already gave.
      setOverview((current) => (full || !current ? counts : { ...current, ...counts }))
    } catch (reason) {
      if (request === overviewRequest.current && reason instanceof ApiError && reason.status === 401) setAuthState('anonymous')
    }
  }, [])

  const deviceRefresh = useThrottledRunner(refreshDevices, DEVICE_REFRESH_MS)
  const overviewRefresh = useThrottledRunner(
    refreshOverview,
    () => FULL_OVERVIEW_ROUTES.has(viewRef.current) ? FLEET_OVERVIEW_REFRESH_MS : BACKGROUND_OVERVIEW_REFRESH_MS,
  )

  const invalidateFleetRequests = useCallback(() => {
    ++deviceRequest.current
    ++overviewRequest.current
    deviceRefresh.cancel()
    overviewRefresh.cancel()
  }, [deviceRefresh, overviewRefresh])

  // Explicit refresh after an operator action: both at once, never overlapping
  // a request already in flight.
  const refreshFleet = useCallback(async () => {
    await Promise.all([deviceRefresh.runNow(), overviewRefresh.runNow()])
  }, [deviceRefresh, overviewRefresh])

  // Runs before handleRealtimeTopics in the same flush.
  const handleRealtimeEvents = useCallback((events: readonly RealtimeEvent[]) => {
    const rows: Device[] = []
    for (const event of events) {
      const connectorId = eventConnector(event)
      if (event.topic === 'attendance' && connectorId) {
        custodyRevisions.current.set(connectorId, (custodyRevisions.current.get(connectorId) || 0) + 1)
      }
      if (!DEVICE_TOPICS.has(event.topic)) continue
      if (connectorId) deviceRevisions.current.set(connectorId, (deviceRevisions.current.get(connectorId) || 0) + 1)
      else unscopedDeviceRevision.current += 1
      if (event.topic !== 'device') continue
      if (isFullDeviceRow(event.data)) rows.push(event.data)
      else deviceListNeeded.current = true
    }
    if (rows.length) deviceSnapshots.put(rows)
  }, [])

  const handleRealtimeTopics = useCallback((topics: ReadonlySet<RealtimeTopic>) => {
    setRevisions((current) => {
      const next = { ...current }
      topics.forEach((topic) => {
        next[topic] += 1
        if (topic === 'resync') {
          ALL_TOPICS.filter((name) => name !== 'resync' && name !== 'backend_error').forEach((name) => { next[name] += 1 })
        }
      })
      return next
    })
    // A device event with a whole row was applied in place; partial ones,
    // command updates and resyncs re-read the list (at most every 5 s).
    const listNeeded = deviceListNeeded.current
    deviceListNeeded.current = false
    if (topics.has('command') || topics.has('resync') || (topics.has('device') && listNeeded)) deviceRefresh.schedule()
    // Attendance never refreshes the overview directly: a punch release emits
    // thousands of batches, and the periodic resync picks the backlog up.
    if (topics.has('alert') || topics.has('resync')) overviewRefresh.schedule()
  }, [deviceRefresh, overviewRefresh])

  const realtime = useRealtime(authState === 'authenticated', handleRealtimeTopics, handleRealtimeEvents)
  const newVersion = useNewVersion(realtime.connections > 1 ? realtime.connections : 0, authState === 'authenticated')

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
      invalidateFleetRequests()
      deviceSnapshots.clear()
      queryClient.clear()
      setOverview(null)
      setDrawer(null)
    }
    window.addEventListener('add:session-expired', handleSessionExpired)
    return () => window.removeEventListener('add:session-expired', handleSessionExpired)
  }, [invalidateFleetRequests])

  useEffect(() => {
    if (authState !== 'authenticated') return
    void refreshFleet()
  }, [authState, refreshFleet])

  // Returning to Home or Devices brings the full figures up to date
  // (immediately when the last overview is older than the 15 s interval).
  const previousView = useRef(pageRoute)
  useEffect(() => {
    if (previousView.current === pageRoute) return
    const wasFull = FULL_OVERVIEW_ROUTES.has(previousView.current)
    previousView.current = pageRoute
    if (authState === 'authenticated' && FULL_OVERVIEW_ROUTES.has(pageRoute) && !wasFull) overviewRefresh.schedule()
  }, [authState, overviewRefresh, pageRoute])

  const login = async (loginUsername: string, password: string) => {
    const response = await api<{ username: string; csrf_token: string }>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: loginUsername, password }),
    })
    setCsrfToken(response.csrf_token)
    setUsername(response.username)
    setAuthState('authenticated')
  }

  const logout = useCallback(async () => {
    try { await api('/api/v1/auth/logout', { method: 'POST', body: '{}' }) } finally {
      setCsrfToken('')
      setAuthState('anonymous')
      invalidateFleetRequests()
      deviceSnapshots.clear()
      queryClient.clear()
      setOverview(null)
    }
  }, [invalidateFleetRequests])

  const manageUsers = useCallback((device: Device) => {
    setSelectedDeviceId(device.connector_id)
    setDrawer(null)
    navigate(routePath('people', device.connector_id, { division }))
  }, [division, navigate])

  // The device panel opens at /devices/:id; closing it returns to the page it
  // was opened from (kept in history state), or the Devices list.
  // From the Devices list the filters (?status=…) are kept, so closing the panel
  // brings the same filtered list back.
  const inspectDevice = useCallback((device: Device) => {
    setDrawer(device)
    const from = panelFrom || `${location.pathname}${location.search}`
    navigate(routePath('devices', device.connector_id, { division }), { state: { from } })
  }, [division, location.pathname, location.search, navigate, panelFrom])

  const closeDevice = useCallback(() => {
    setDrawer(null)
    const from = (location.state as { from?: string } | null)?.from
    navigate(from && from.startsWith('/') && !from.startsWith('//') ? from : routePath('devices', undefined, { division }))
  }, [division, location.state, navigate])

  const selectUserDevice = useCallback((id: string) => {
    setSelectedDeviceId(id)
    navigate(id ? routePath('people', id, { division }) : routePath('people', undefined, { division }))
  }, [division, navigate])

  const deviceRevision = useCallback((connectorId: string) =>
    (deviceRevisions.current.get(connectorId) || 0) + unscopedDeviceRevision.current + revisions.resync,
  // revisions changes on every flush, so the refs are read fresh each time.
  [revisions])
  const deviceCustodyRevision = useCallback((connectorId: string) =>
    (custodyRevisions.current.get(connectorId) || 0) + revisions.reconciliation,
  [revisions])

  const appData = useMemo<AppData>(() => ({
    username,
    devices,
    devicesLoaded,
    devicesError,
    devicesUpdatedAt,
    overview,
    loading: !devicesLoaded,
    revisions,
    deviceRevision,
    deviceCustodyRevision,
    realtime: { state: realtime.state, lastSyncAt: realtime.lastSyncAt },
    toast,
    refreshFleet,
    selectedDeviceId,
    selectUserDevice,
    inspectDevice,
    manageUsers,
    navigate: (to: string) => navigate(to),
    logout,
  }), [
    deviceCustodyRevision, deviceRevision, devices, devicesError, devicesLoaded, devicesUpdatedAt, inspectDevice,
    logout, manageUsers, navigate, overview, realtime.lastSyncAt, realtime.state, refreshFleet, revisions, selectUserDevice,
    selectedDeviceId, toast, username,
  ])

  if (authState === 'loading') return <main className="boot-screen"><img src="/state-life-logo.png" alt="State Life Insurance Corporation" /><p>Opening Attendance Device Dashboard…</p></main>
  if (authState === 'anonymous') return <Suspense fallback={<main className="boot-screen"><img src="/state-life-logo.png" alt="State Life Insurance Corporation" /><p>Opening secure sign in…</p></main>}><Login onLogin={login} /></Suspense>

  const entry = routeTable[pageRoute]
  const Page = entry.Page
  // The badge stays hidden until the first overview: never a false "0".
  const openAlertCount = overview ? overview.open_unacknowledged_alerts ?? overview.open_alerts : null
  return (
    <AppDataProvider value={appData}>
      <AppShell workspaceRef={workspaceRef} username={username} route={pageRoute} openAlertCount={openAlertCount} onNavigate={setView} onLogout={logout} realtimeState={realtime.state} lastSyncAt={realtime.lastSyncAt} fetchFailed={Boolean(devicesError) && devicesLoaded} onTryNow={refreshFleet}>
        <NewVersionNotice available={newVersion} />
        <PageErrorBoundary key={pageRoute}>
          <Suspense fallback={<WorkspaceLoading label={entry.loadingLabel} />}><Page /></Suspense>
        </PageErrorBoundary>
      </AppShell>
      {drawer && <PageErrorBoundary key={drawer.connector_id}><Suspense fallback={null}><DeviceDrawer key={drawer.connector_id} seed={drawer} revision={deviceRevision(drawer.connector_id)} custodyRevision={deviceCustodyRevision(drawer.connector_id)} onClose={closeDevice} onManageUsers={manageUsers} onInventoryChanged={refreshFleet} toast={toast} /></Suspense></PageErrorBoundary>}
    </AppDataProvider>
  )
}

// Waits briefly so a fast lazy chunk never flashes a loading state.
function WorkspaceLoading({ label }: { label: string }) {
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    const timer = window.setTimeout(() => setVisible(true), 180)
    return () => window.clearTimeout(timer)
  }, [])
  if (!visible) return null
  return <div className="panel empty-state is-loading" role="status"><Icon name="refresh" /><h3>{label}…</h3></div>
}

export default function App() {
  const router = useMemo(() => createBrowserRouter([{
    path: '*',
    element: <DashboardApp />,
    errorElement: <RouteErrorBoundary />,
  }]), [])
  const [prefs] = usePrefs()
  // ToastProvider shows messages in #overlay-root for the whole app (FND-6);
  // "Show technical details" (Settings, add.prefs) opens every Technical details box.
  return (
    <TechnicalDetailsProvider show={prefs.technical}>
      <ToastProvider><RouterProvider router={router} /></ToastProvider>
    </TechnicalDetailsProvider>
  )
}
