import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react'
import './Attendance.css'
import { api, queryString } from '../api'
import {
  Metric, PageHeader, StatusBadge, dateTime, pktInputToUtc, pktTodayBounds,
  relativeTime, statusPattern,
} from '../App'
import { Icon } from '../Icon'
import type { RealtimeState } from '../realtime'
import type { AttendanceEvent, Device } from '../types'
import { AttendanceReleaseHistory, AttendanceReleaseReview } from './AttendanceRelease'

type AttendanceRange = 'latest' | 'today' | 'last24' | 'custom'
type AttendanceViewMode = 'all-events' | 'needs-review' | 'release-history'
type AttendanceFilters = {
  device_id: string
  q: string
  cnic: string
  punch: string
  clock_quality: string
  source: string
  from_time: string
  to_time: string
}
type AttendanceResponse = { rows: AttendanceEvent[]; next_cursor: number | null }

const emptyFilters: AttendanceFilters = {
  device_id: '', q: '', cnic: '', punch: '', clock_quality: '', source: '',
  from_time: '', to_time: '',
}

const sourceLabels: Record<string, string> = {
  LIVE: 'Live capture',
  LIVE_POLL: 'Live poll',
  FULL_HISTORY: 'Full history',
  CURRENT_RECONCILE: 'Current reconcile',
  RECONCILE_15M: 'Scheduled reconcile',
  DUMP_RECONNECT: 'Reconnect recovery',
  DUMP_STARTUP: 'Startup recovery',
}

const punchLabel = (punch: string | null) => {
  if (punch === '0') return 'Check in'
  if (punch === '1') return 'Check out'
  return punch == null || punch === '' ? 'Punch not reported' : `Punch ${punch}`
}

const captureLabel = (source: string) =>
  sourceLabels[source] || source.replaceAll('_', ' ').toLowerCase().replace(/^./, (value) => value.toUpperCase())

const pktInputValue = (value: Date) => {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Karachi', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(value)
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value || ''
  return `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}`
}

const mergeByEventUid = (first: AttendanceEvent[], second: AttendanceEvent[]) => {
  const rows = new Map<string, AttendanceEvent>()
  ;[...first, ...second].forEach((row) => {
    if (!rows.has(row.event_uid)) rows.set(row.event_uid, row)
  })
  return [...rows.values()]
}

function AttendanceSkeleton() {
  return (
    <div className="attendance-skeleton" role="status" aria-label="Loading live attendance">
      {Array.from({ length: 6 }, (_, index) => (
        <span key={index}><i /><i /><i /></span>
      ))}
    </div>
  )
}

function AllAttendanceEvents({
  devices,
  revision,
  realtimeState,
  realtimeLastSyncAt,
  onReviewEmployee,
}: {
  devices: Device[]
  revision: number
  realtimeState: RealtimeState
  realtimeLastSyncAt: Date | null
  onReviewEmployee: (row: AttendanceEvent) => void
}) {
  const [rows, setRows] = useState<AttendanceEvent[]>([])
  const rowsRef = useRef(rows)
  rowsRef.current = rows
  const [pendingRows, setPendingRows] = useState<AttendanceEvent[]>([])
  const [nextCursor, setNextCursor] = useState<number | null>(null)
  const [filters, setFilters] = useState<AttendanceFilters>(emptyFilters)
  const [range, setRange] = useState<AttendanceRange>('latest')
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [live, setLive] = useState(true)
  const liveRef = useRef(live)
  liveRef.current = live
  const [topVisible, setTopVisible] = useState(true)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [error, setError] = useState('')
  const [lastUpdatedAt, setLastUpdatedAt] = useState<Date | null>(null)
  const [announcement, setAnnouncement] = useState('Live attendance is starting.')
  const latestRequest = useRef<AbortController | null>(null)
  const pageRequest = useRef<AbortController | null>(null)
  const topSentinel = useRef<HTMLDivElement>(null)
  const initialRevision = useRef(revision)

  const validationError = useMemo(() => {
    if (filters.cnic && !/^\d{13}$/.test(filters.cnic)) return 'Enter an exact 13-digit CNIC before querying.'
    if (filters.from_time && filters.to_time && filters.from_time > filters.to_time) {
      return 'The From time must be earlier than or equal to the To time.'
    }
    return ''
  }, [filters.cnic, filters.from_time, filters.to_time])

  const requestPath = useCallback((cursor?: number) => `/api/v1/attendance${queryString({
    ...filters,
    from_time: pktInputToUtc(filters.from_time),
    to_time: pktInputToUtc(filters.to_time),
    cursor,
    limit: 100,
  })}`, [filters])

  const replaceFeed = useCallback(async () => {
    if (validationError) {
      latestRequest.current?.abort()
      setLoading(false)
      setError(validationError)
      return
    }
    latestRequest.current?.abort()
    pageRequest.current?.abort()
    const controller = new AbortController()
    latestRequest.current = controller
    setLoading(true)
    setError('')
    setPendingRows([])
    try {
      const response = await api<AttendanceResponse>(requestPath(), { signal: controller.signal })
      if (controller.signal.aborted || latestRequest.current !== controller) return
      setRows(response.rows)
      setNextCursor(response.next_cursor ?? null)
      setLastUpdatedAt(new Date())
      setAnnouncement(`${response.rows.length} attendance events loaded.`)
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setError(reason instanceof Error ? reason.message : 'Attendance could not be loaded.')
    } finally {
      if (latestRequest.current === controller) setLoading(false)
    }
  }, [requestPath, validationError])

  const refreshFeed = useCallback(async (reveal = false) => {
    if (validationError) {
      setError(validationError)
      return
    }
    latestRequest.current?.abort()
    const controller = new AbortController()
    latestRequest.current = controller
    setRefreshing(true)
    setError('')
    try {
      const response = await api<AttendanceResponse>(requestPath(), { signal: controller.signal })
      if (controller.signal.aborted || latestRequest.current !== controller) return
      const current = rowsRef.current
      const currentIds = new Set(current.map((row) => row.event_uid))
      const unseen = response.rows.filter((row) => !currentIds.has(row.event_uid))
      const newestById = new Map(response.rows.map((row) => [row.event_uid, row]))
      const refreshedCurrent = current.map((row) => newestById.get(row.event_uid) || row)
      if (reveal || topVisible) {
        setRows(mergeByEventUid(unseen, refreshedCurrent))
        setPendingRows([])
        setAnnouncement(unseen.length ? `${unseen.length} new attendance events added.` : 'Live attendance is up to date.')
      } else {
        setRows(refreshedCurrent)
        setPendingRows((queued) => mergeByEventUid(unseen, queued))
        setAnnouncement(unseen.length ? `${unseen.length} new attendance events are ready to review.` : 'Live attendance is up to date.')
      }
      setLastUpdatedAt(new Date())
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setError(reason instanceof Error ? reason.message : 'Live attendance could not be refreshed.')
    } finally {
      if (latestRequest.current === controller) setRefreshing(false)
    }
  }, [requestPath, topVisible, validationError])

  const loadOlder = async () => {
    if (!nextCursor || validationError) return
    pageRequest.current?.abort()
    const controller = new AbortController()
    pageRequest.current = controller
    setLoadingMore(true)
    setError('')
    try {
      const response = await api<AttendanceResponse>(requestPath(nextCursor), { signal: controller.signal })
      if (controller.signal.aborted || pageRequest.current !== controller) return
      setRows((current) => mergeByEventUid(current, response.rows))
      setNextCursor(response.next_cursor ?? null)
      setAnnouncement(`${response.rows.length} older attendance events loaded.`)
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setError(reason instanceof Error ? reason.message : 'Older attendance could not be loaded.')
    } finally {
      if (pageRequest.current === controller) setLoadingMore(false)
    }
  }

  useEffect(() => {
    const timeout = window.setTimeout(() => { if (liveRef.current) void replaceFeed() }, 300)
    return () => window.clearTimeout(timeout)
  }, [replaceFeed])

  useEffect(() => () => {
    latestRequest.current?.abort()
    pageRequest.current?.abort()
  }, [])

  useEffect(() => {
    if (revision === initialRevision.current) return
    initialRevision.current = revision
    if (live) void refreshFeed(false)
  }, [live, refreshFeed, revision])

  useEffect(() => {
    if (!topSentinel.current || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(([entry]) => setTopVisible(entry.isIntersecting), { threshold: 0.7 })
    observer.observe(topSentinel.current)
    return () => observer.disconnect()
  }, [rows.length])

  const setRangePreset = (value: AttendanceRange) => {
    setRange(value)
    if (value === 'latest') {
      setFilters((current) => ({ ...current, from_time: '', to_time: '' }))
    } else if (value === 'today') {
      const bounds = pktTodayBounds()
      setFilters((current) => ({ ...current, from_time: bounds.from, to_time: bounds.to }))
    } else if (value === 'last24') {
      const now = new Date()
      setFilters((current) => ({
        ...current,
        from_time: pktInputValue(new Date(now.getTime() - 24 * 60 * 60 * 1000)),
        to_time: pktInputValue(now),
      }))
    } else if (!filters.from_time && !filters.to_time) {
      const bounds = pktTodayBounds()
      setFilters((current) => ({ ...current, from_time: bounds.from, to_time: bounds.to }))
    }
  }

  const resetFilters = () => {
    setRange('latest')
    setFilters(emptyFilters)
    setAdvancedOpen(false)
  }

  const removeFilter = (key: keyof AttendanceFilters) => {
    setFilters((current) => ({ ...current, [key]: '' }))
    if (key === 'from_time' || key === 'to_time') setRange('custom')
  }

  const revealPending = () => {
    setRows((current) => mergeByEventUid(pendingRows, current))
    setAnnouncement(`${pendingRows.length} new attendance events added.`)
    setPendingRows([])
    topSentinel.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }

  const toggleLive = () => {
    if (live) {
      liveRef.current = false
      latestRequest.current?.abort()
      pageRequest.current?.abort()
      setLoading(false)
      setRefreshing(false)
      setLoadingMore(false)
      setLive(false)
      setAnnouncement('Live attendance updates paused.')
    } else {
      liveRef.current = true
      setLive(true)
      setAnnouncement('Live attendance resumed.')
      void refreshFeed(false)
    }
  }

  const deviceBySerial = useMemo(
    () => new Map(devices.flatMap((device) => device.zkt?.serial ? [[device.zkt.serial, device] as const] : [])),
    [devices],
  )
  const confirmed = rows.filter((row) => Boolean(row.oracle_confirmed_at) || row.ords_status.startsWith('ACK')).length
  const dataQualityAttention = rows.filter((row) =>
    !row.display_name || !row.cnic_masked || row.clock_quality !== 'OK',
  ).length
  const filtered = Object.entries(filters).filter(([, value]) => Boolean(value)) as Array<[keyof AttendanceFilters, string]>
  const filterLabels: Partial<Record<keyof AttendanceFilters, string>> = {
    device_id: 'Device', q: 'Search', cnic: 'Exact CNIC', punch: 'Punch',
    clock_quality: 'Clock', source: 'Capture', from_time: 'From', to_time: 'To',
  }
  const connectionLabel = {
    connecting: 'Connecting', live: 'Realtime connected', reconnecting: 'Reconnecting', stale: 'Cached connection',
  }[realtimeState]

  return (
    <div className="attendance-all-events">
      <PageHeader
        eyebrow="IMMUTABLE CAPTURE LEDGER"
        title="Live attendance"
        description="Follow the newest punches as they arrive while preserving every terminal and Oracle outcome."
        action={
          <div className="attendance-live-actions">
            <span className={`attendance-live-state ${live ? 'is-live' : 'is-paused'}`} title={realtimeLastSyncAt ? `Realtime last synchronized ${realtimeLastSyncAt.toLocaleString('en-PK', { timeZone: 'Asia/Karachi' })} PKT` : connectionLabel}><i /> {live ? connectionLabel : 'Updates paused'}</span>
            <button className="button secondary" type="button" onClick={toggleLive}><Icon name={live ? 'pause' : 'pulse'} /> {live ? 'Pause' : 'Resume'}</button>
            <button className="button secondary" type="button" disabled={refreshing} onClick={() => void refreshFeed(true)}><Icon name="refresh" /> {refreshing ? 'Refreshing…' : 'Refresh'}</button>
          </div>
        }
      />

      <section className="attendance-status-strip" aria-label="Live attendance status">
        <div><span className={live ? 'status-dot live' : 'status-dot'} /><strong>{live ? 'Monitoring newest events' : 'Feed held in place'}</strong><small>{lastUpdatedAt ? `Last updated ${lastUpdatedAt.toLocaleTimeString('en-PK', { timeZone: 'Asia/Karachi' })} PKT` : 'Connecting to the immutable ledger'}</small></div>
        <span><Icon name="shield" /> Read-only · no terminal history can be changed here</span>
      </section>

      <section className="metric-grid attendance-metrics" aria-label="Loaded attendance results">
        <Metric label="Events loaded" value={rows.length.toLocaleString()} detail="Current browser result set" icon="clock" />
        <Metric label="Oracle confirmed" value={confirmed.toLocaleString()} detail="Within loaded results" icon="check" tone="positive" />
        <Metric label="Awaiting confirmation" value={(rows.length - confirmed).toLocaleString()} detail="Within loaded results" icon="refresh" tone={rows.length - confirmed ? 'warning' : 'positive'} />
        <Metric label="Data-quality attention" value={dataQualityAttention.toLocaleString()} detail="Missing identity/CNIC or clock concern" icon="alert" tone={dataQualityAttention ? 'warning' : 'positive'} />
      </section>

      <section className="panel attendance-feed-panel">
        <header className="panel-header attendance-feed-header">
          <div><h2>Newest attendance events</h2><p>All capture sources remain in one trustworthy chronology.</p></div>
          <div className="attendance-range" role="group" aria-label="Attendance range">
            {([
              ['latest', 'Latest'], ['today', 'Today (PKT)'], ['last24', 'Last 24 hours'], ['custom', 'Custom'],
            ] as Array<[AttendanceRange, string]>).map(([value, label]) => (
              <button key={value} type="button" className={range === value ? 'active' : ''} aria-pressed={range === value} onClick={() => setRangePreset(value)}>{label}</button>
            ))}
          </div>
        </header>

        <div className="attendance-primary-filters">
          <label className="search-field"><span className="sr-only">Search employee name, user ID, or UID</span><Icon name="search" /><input value={filters.q} onChange={(event) => setFilters({ ...filters, q: event.target.value })} placeholder="Search employee, user ID, or UID" /></label>
          <label><span className="sr-only">Attendance device</span><select value={filters.device_id} onChange={(event) => setFilters({ ...filters, device_id: event.target.value })}><option value="">All devices</option>{devices.map((device) => <option key={device.connector_id} value={device.connector_id}>{device.display_name}</option>)}</select></label>
          <details className="attendance-advanced" open={advancedOpen} onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}>
            <summary><Icon name="menu" /> Filters {filtered.length > 0 && <span>{filtered.length}</span>}</summary>
            <div className="attendance-advanced-body">
              <header><div><p className="eyebrow">FILTER LIVE ATTENDANCE</p><h3>Advanced filters</h3></div><button className="icon-button" type="button" aria-label="Close attendance filters" onClick={() => setAdvancedOpen(false)}><Icon name="x" /></button></header>
              <div className="attendance-advanced-grid">
                <label>Exact CNIC<input inputMode="numeric" autoComplete="off" value={filters.cnic} onChange={(event) => setFilters({ ...filters, cnic: event.target.value.replace(/\D/g, '').slice(0, 13) })} placeholder="13 digits" /></label>
                <label>Punch<select value={filters.punch} onChange={(event) => setFilters({ ...filters, punch: event.target.value })}><option value="">All punches</option><option value="0">Check in</option><option value="1">Check out</option></select></label>
                <label>Clock quality<select value={filters.clock_quality} onChange={(event) => setFilters({ ...filters, clock_quality: event.target.value })}><option value="">Any quality</option><option value="OK">OK</option><option value="DRIFTED">Drifted</option><option value="UNKNOWN">Unknown</option></select></label>
                <label>Capture source<select value={filters.source} onChange={(event) => setFilters({ ...filters, source: event.target.value })}><option value="">All sources</option>{Object.entries(sourceLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                <label>From (PKT)<input type="datetime-local" value={filters.from_time} onChange={(event) => { setRange('custom'); setFilters({ ...filters, from_time: event.target.value }) }} /></label>
                <label>To (PKT)<input type="datetime-local" value={filters.to_time} onChange={(event) => { setRange('custom'); setFilters({ ...filters, to_time: event.target.value }) }} /></label>
              </div>
              <footer><button className="button secondary" type="button" onClick={resetFilters}><Icon name="x" /> Clear all</button><button className="button primary" type="button" onClick={() => setAdvancedOpen(false)}>View results</button></footer>
            </div>
          </details>
        </div>

        {filtered.length > 0 && (
          <div className="active-filter-bar" aria-label="Active attendance filters">
            <span>{filtered.length} active</span>
            {filtered.map(([key, value]) => <button type="button" key={key} onClick={() => removeFilter(key)}>{filterLabels[key]}: {key === 'device_id' ? devices.find((device) => device.connector_id === value)?.display_name || value : key === 'source' ? captureLabel(value) : key === 'punch' ? punchLabel(value) : value}<Icon name="x" /></button>)}
            <button className="text-button" type="button" onClick={resetFilters}>Clear all</button>
          </div>
        )}

        {(error || validationError) && <div className="message pattern-blocked operational-error" role="alert"><Icon name="alert" /><span>{validationError || error}</span><button className="button secondary" type="button" onClick={() => void replaceFeed()}>Retry</button></div>}
        <div className="sr-only" aria-live="polite">{announcement}</div>
        <div ref={topSentinel} className="attendance-top-sentinel" aria-hidden="true" />
        {pendingRows.length > 0 && <button className="attendance-new-events" type="button" onClick={revealPending}><Icon name="pulse" /> {pendingRows.length} new event{pendingRows.length === 1 ? '' : 's'} · Show newest</button>}

        <div className="attendance-event-list" aria-busy={loading || refreshing} aria-label="Live attendance events">
          <div className="attendance-event-head" aria-hidden="true"><span>Employee</span><span>Event</span><span>Terminal</span><span>Capture</span><span>Oracle delivery</span><span /></div>
          {loading && !rows.length && <AttendanceSkeleton />}
          {rows.map((row) => {
            const terminal = row.device_serial ? deviceBySerial.get(row.device_serial) : undefined
            const rowAttention = !row.display_name || !row.cnic_masked || row.clock_quality !== 'OK' || statusPattern(row.ords_status) === 'blocked' || row.release_state === 'LOCKED'
            return (
              <article className={`attendance-event ${rowAttention ? 'attendance-event-attention' : ''}`} key={row.event_uid} aria-label={`${row.display_name || 'Unknown identity'}, ${punchLabel(row.punch)}, ${dateTime(row.device_event_time)}`}>
                <div className="attendance-event-cell attendance-person" data-label="Employee"><span className="avatar">{(row.display_name || '?').slice(0, 2).toUpperCase()}</span><span><strong>{row.display_name || 'Unknown identity'}</strong><small>{row.cnic_masked || `User ${row.user_id} · identity incomplete`}</small></span></div>
                <div className="attendance-event-cell" data-label="Event"><strong>{punchLabel(row.punch)}</strong><small>{dateTime(row.device_event_time)} · received {relativeTime(row.received_at)}</small></div>
                <div className="attendance-event-cell" data-label="Terminal"><strong>{terminal?.display_name || row.device_serial || 'Unreported terminal'}</strong><small>{terminal ? row.device_serial : 'Device name unavailable'}</small></div>
                <div className="attendance-event-cell attendance-status-stack" data-label="Capture"><StatusBadge state={captureLabel(row.source)} /><small className={row.clock_quality === 'OK' ? '' : 'attention-copy'}>{row.clock_quality === 'OK' ? 'Clock verified' : `Clock ${row.clock_quality.toLowerCase()}`}</small></div>
                <div className="attendance-event-cell attendance-status-stack" data-label="Oracle delivery">
                  <StatusBadge state={row.ords_status} />
                  {row.release_state && row.release_state !== 'NOT_APPLICABLE' && <StatusBadge state={row.release_state_label || row.release_state} />}
                  <small>{row.release_state === 'RELEASED' && row.effective_identity_downstream_confirmed_at ? `Oracle and downstream verified ${relativeTime(row.effective_identity_downstream_confirmed_at)}` : row.oracle_confirmed_at ? `Original disposition confirmed ${relativeTime(row.oracle_confirmed_at)}` : row.release_lock_reason ? explainReleaseLock(row.release_lock_reason) : 'Confirmation pending'}</small>
                  {row.release_state === 'ELIGIBLE' && row.release_target_user_key && row.release_connector_id && <button className="text-button" type="button" onClick={() => onReviewEmployee(row)}>Review employee</button>}
                </div>
                <details className="attendance-event-details"><summary aria-label={`View event details for ${row.display_name || `user ${row.user_id}`}`}><Icon name="chevron" /></summary><div><span><small>Terminal IDs</small><strong>UID {row.uid || '—'} · User {row.user_id}</strong></span><span><small>Captured / received</small><strong>{dateTime(row.captured_at)} / {dateTime(row.received_at)}</strong></span><span><small>Clock evidence</small><strong>{row.clock_drift_seconds == null ? row.clock_quality : `${Math.round(row.clock_drift_seconds)}s drift · ${row.clock_quality}`}</strong></span><span><small>Event UID</small><code>{row.event_uid}</code></span><span><small>Original Oracle disposition</small><strong>{row.ords_status.replaceAll('_', ' ').toLowerCase()}</strong></span><span><small>Effective release state</small><strong>{row.release_state_label || 'Not released'}{row.latest_release_job_id ? ` · job ${row.latest_release_job_id}` : ''}</strong></span></div></details>
              </article>
            )
          })}
          {!loading && !rows.length && !error && !validationError && <div className="empty-state"><Icon name="clock" /><h3>{filtered.length ? 'No attendance matches these filters.' : 'No attendance events have arrived yet.'}</h3><p>{filtered.length ? 'Remove filters or choose a wider PKT range.' : 'Live events will appear here without changing terminal history.'}</p>{filtered.length > 0 && <button className="button secondary" type="button" onClick={resetFilters}>Clear filters</button>}</div>}
        </div>
        {nextCursor && <div className="load-more"><button className="button secondary" type="button" disabled={loadingMore} onClick={() => void loadOlder()}>{loadingMore ? 'Loading older events…' : 'Load older events'}</button><small>{rows.length.toLocaleString()} events loaded in this browser</small></div>}
      </section>
    </div>
  )
}

const explainReleaseLock = (code: string) =>
  ({
    TARGET_CNIC_MISSING: 'Add a valid CNIC before release',
    TARGET_NOT_ACTIVE: 'Employee is not active',
    TARGET_SNAPSHOT_UNSTABLE: 'Terminal employee snapshot is unstable',
    IDENTITY_SNAPSHOT_UNSTABLE: 'Terminal employee snapshot is unstable',
    TARGET_DUPLICATE_CNIC_UNRESOLVED: 'Duplicate CNIC conflict must be resolved',
    CLOCK_NOT_OK: 'Invalid terminal time remains locked',
    QUARANTINED_INVALID_DEVICE_TIME: 'Invalid terminal time remains locked',
    QUARANTINED_INVALID_EVENT_UID: 'Invalid event UID remains locked',
    QUARANTINED_ORDS_REJECTED: 'Oracle-rejected punch remains locked',
    SOURCE_RECERTIFICATION_REQUIRED: 'Fresh terminal source certificate required',
    TERMINAL_RELEASE_IN_PROGRESS: 'Another release is active on this terminal',
  } as Record<string, string>)[code] || code.replaceAll('_', ' ').toLowerCase()

export function AttendanceView({
  devices,
  revision,
  realtimeState,
  realtimeLastSyncAt,
  toast,
}: {
  devices: Device[]
  revision: number
  realtimeState: RealtimeState
  realtimeLastSyncAt: Date | null
  toast?: { notice: (message: string) => void; error: (message: string) => void }
}) {
  const initial = useMemo(() => new URLSearchParams(window.location.search), [])
  const initialMode = initial.get('view')
  const [mode, setMode] = useState<AttendanceViewMode>(
    initialMode === 'needs-review' || initialMode === 'release-history'
      ? initialMode
      : 'all-events',
  )
  const [reviewConnectorId, setReviewConnectorId] = useState(initial.get('device_id'))
  const [reviewUserKey, setReviewUserKey] = useState(initial.get('user_key'))
  const [releaseJobId, setReleaseJobId] = useState(initial.get('release_job'))
  const safeToast = useMemo(
    () => toast || { notice: () => undefined, error: () => undefined },
    [toast],
  )
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])
  const tabs = [
    { id: 'all-events', label: 'All events', icon: 'pulse' },
    { id: 'needs-review', label: 'Needs review', icon: 'shield' },
    { id: 'release-history', label: 'Release history', icon: 'list' },
  ] as const

  const navigateMode = useCallback((
    next: AttendanceViewMode,
    values?: { connectorId?: string | null; userKey?: string | null; jobId?: string | null },
  ) => {
    const params = new URLSearchParams(window.location.search)
    params.set('view', next)
    params.delete('device_id')
    params.delete('user_key')
    params.delete('release_job')
    if (values?.connectorId) params.set('device_id', values.connectorId)
    if (values?.userKey) params.set('user_key', values.userKey)
    if (values?.jobId) params.set('release_job', values.jobId)
    window.history.pushState(null, '', `${window.location.pathname}?${params}`)
    setReviewConnectorId(values?.connectorId || null)
    setReviewUserKey(values?.userKey || null)
    setReleaseJobId(values?.jobId || null)
    setMode(next)
  }, [])

  useEffect(() => {
    const synchronizeFromLocation = () => {
      const params = new URLSearchParams(window.location.search)
      const requested = params.get('view')
      const next: AttendanceViewMode =
        requested === 'needs-review' || requested === 'release-history'
          ? requested
          : 'all-events'
      setReviewConnectorId(params.get('device_id'))
      setReviewUserKey(params.get('user_key'))
      setReleaseJobId(params.get('release_job'))
      setMode(next)
    }
    window.addEventListener('popstate', synchronizeFromLocation)
    return () => window.removeEventListener('popstate', synchronizeFromLocation)
  }, [])

  const moveTab = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    let next = index
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
    else if (event.key === 'ArrowLeft')
      next = (index - 1 + tabs.length) % tabs.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = tabs.length - 1
    else return
    event.preventDefault()
    navigateMode(tabs[next].id)
    tabRefs.current[next]?.focus()
  }

  const reviewEmployee = (row: AttendanceEvent) => {
    navigateMode('needs-review', {
      connectorId: row.release_connector_id,
      userKey: row.release_target_user_key,
    })
  }

  return (
    <div className="attendance-workspace">
      {mode !== 'all-events' && (
        <PageHeader
          eyebrow="CONTROLLED ORDS RELEASE"
          title={mode === 'needs-review' ? 'Attendance · Needs review' : 'Attendance · Release history'}
          description={mode === 'needs-review' ? 'Review exact identity-held punches for one current employee and terminal at a time.' : 'Trace approvals, Oracle receipts, retries, downstream proof and every per-punch outcome.'}
        />
      )}
      <nav className="attendance-view-tabs" role="tablist" aria-label="Attendance views">
        {tabs.map((tab, index) => (
          <button
            key={tab.id}
            ref={(node) => { tabRefs.current[index] = node }}
            type="button"
            role="tab"
            id={`attendance-${tab.id}-tab`}
            aria-controls={`attendance-${tab.id}-panel`}
            aria-selected={mode === tab.id}
            tabIndex={mode === tab.id ? 0 : -1}
            onKeyDown={(event) => moveTab(event, index)}
            onClick={() => navigateMode(tab.id)}
          >
            <Icon name={tab.icon} /> {tab.label}
          </button>
        ))}
      </nav>
      <div
        id={`attendance-${mode}-panel`}
        role="tabpanel"
        aria-labelledby={`attendance-${mode}-tab`}
      >
        {mode === 'all-events' && <AllAttendanceEvents devices={devices} revision={revision} realtimeState={realtimeState} realtimeLastSyncAt={realtimeLastSyncAt} onReviewEmployee={reviewEmployee} />}
        {mode === 'needs-review' && <AttendanceReleaseReview devices={devices} revision={revision} toast={safeToast} initialConnectorId={reviewConnectorId} initialUserKey={reviewUserKey} onOpenHistory={(jobId) => navigateMode('release-history', { jobId })} />}
        {mode === 'release-history' && <AttendanceReleaseHistory devices={devices} revision={revision} toast={safeToast} initialJobId={releaseJobId} />}
      </div>
    </div>
  )
}
