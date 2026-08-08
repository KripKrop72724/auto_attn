import { useCallback, useEffect, useRef, useState } from 'react'
import { api, queryString } from '../api'
import {
  PageHeader, StatusBadge, dateTime, formatAlertDiagnostics, pktInputToUtc,
  pktTodayBounds, relativeTime, statusPattern, useToast,
} from '../App'
import { Icon } from '../Icon'
import { routePath } from '../routing'
import type { Alert, AlertQueueResponse, AttendanceEvent, Device } from '../types'

export function AttendanceView({ devices, revision }: { devices: Device[]; revision: number }) {
  const [rows, setRows] = useState<AttendanceEvent[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [nextCursor, setNextCursor] = useState<number | null>(null)
  const [filters, setFilters] = useState({ device_id: '', q: '', cnic: '', punch: '', clock_quality: '', from_time: '', to_time: '' })
  const requestRef = useRef<AbortController | null>(null)
  const load = useCallback(async (cursor?: number, append = false) => {
    if (filters.cnic && !/^\d{13}$/.test(filters.cnic)) {
      setError('Enter an exact 13-digit CNIC before querying.')
      return
    }
    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    setLoading(true)
    setError('')
    try {
      const response = await api<{ rows: AttendanceEvent[]; next_cursor: number | null }>(`/api/v1/attendance${queryString({
        ...filters,
        from_time: pktInputToUtc(filters.from_time),
        to_time: pktInputToUtc(filters.to_time),
        cursor,
        limit: 100,
      })}`, { signal: controller.signal })
      setRows((current) => append ? [...current, ...response.rows] : response.rows)
      setNextCursor(response.next_cursor ?? null)
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setError(reason instanceof Error ? reason.message : 'Attendance could not be loaded.')
    } finally {
      if (requestRef.current === controller) setLoading(false)
    }
  }, [filters])
  useEffect(() => {
    const timeout = window.setTimeout(() => void load(undefined, false), 300)
    return () => { window.clearTimeout(timeout); requestRef.current?.abort() }
  }, [load, revision])
  const reset = () => setFilters({ device_id: '', q: '', cnic: '', punch: '', clock_quality: '', from_time: '', to_time: '' })
  const today = () => {
    const { from, to } = pktTodayBounds()
    setFilters((current) => ({ ...current, from_time: from, to_time: to }))
  }
  const activeFilterCount = Object.values(filters).filter(Boolean).length
  return (
    <>
      <PageHeader eyebrow="IMMUTABLE CAPTURE LEDGER" title="Attendance events" description="Review live and reconciled punches without changing terminal history." action={<button className="button secondary" onClick={() => void load(undefined, false)}><Icon name="refresh" /> Refresh</button>} />
      <section className="panel">
        <div className="filter-actions"><div><button className="filter-chip" onClick={today}>Today</button><span className="filter-summary">{activeFilterCount ? `${activeFilterCount} filters applied` : 'All attendance events'}</span></div>{activeFilterCount > 0 && <button className="text-button" onClick={reset}><Icon name="x" /> Clear filters</button>}</div>
        <div className="filter-grid">
          <label>Device<select value={filters.device_id} onChange={(event) => setFilters({ ...filters, device_id: event.target.value })}><option value="">All devices</option>{devices.map((device) => <option key={device.connector_id} value={device.connector_id}>{device.display_name}</option>)}</select></label>
          <label>Name / user ID<input value={filters.q} onChange={(event) => setFilters({ ...filters, q: event.target.value })} /></label>
          <label>Exact CNIC<input inputMode="numeric" value={filters.cnic} onChange={(event) => setFilters({ ...filters, cnic: event.target.value.replace(/\D/g, '').slice(0, 13) })} placeholder="13 digits" /></label>
          <label>Punch<select value={filters.punch} onChange={(event) => setFilters({ ...filters, punch: event.target.value })}><option value="">All punches</option><option value="0">Check in</option><option value="1">Check out</option></select></label>
          <label>Clock quality<select value={filters.clock_quality} onChange={(event) => setFilters({ ...filters, clock_quality: event.target.value })}><option value="">Any quality</option><option value="OK">OK</option><option value="DRIFTED">Drifted</option><option value="UNKNOWN">Unknown</option></select></label>
          <label>From (PKT)<input type="datetime-local" value={filters.from_time} onChange={(event) => setFilters({ ...filters, from_time: event.target.value })} /></label>
          <label>To (PKT)<input type="datetime-local" value={filters.to_time} onChange={(event) => setFilters({ ...filters, to_time: event.target.value })} /></label>
        </div>
        {error && <div className="message pattern-blocked operational-error" role="alert"><Icon name="alert" /><span>{error}</span><button className="button secondary" onClick={() => void load(undefined, false)}>Retry</button></div>}
        <div className="attendance-table">
          <div className="attendance-head"><span>Employee</span><span>Event time</span><span>Terminal</span><span>Capture</span><span>Delivery</span></div>
          {loading && !rows.length && <div className="empty-state compact">Loading attendance ledger…</div>}
          {rows.map((row) => <article key={row.event_uid}><div><strong>{row.display_name || 'Unknown identity'}</strong><small>{row.cnic_masked || `User ${row.user_id}`}</small></div><div><strong>{dateTime(row.device_event_time)}</strong><small>Received {relativeTime(row.received_at)}</small></div><div><strong>{row.device_serial || 'Unreported serial'}</strong><small>UID {row.uid || '—'} · User {row.user_id}</small></div><div><StatusBadge state={row.source} /><small>Punch {row.punch ?? '—'} · {row.clock_quality}</small></div><div><StatusBadge state={row.ords_status} /><small>{row.oracle_confirmed_at ? `Oracle confirmed ${relativeTime(row.oracle_confirmed_at)} via ${(row.oracle_confirmation_path || 'unknown path').replaceAll('_', ' ').toLowerCase()}` : row.clock_drift_seconds == null ? 'No Oracle confirmation yet' : `${Math.round(row.clock_drift_seconds)}s clock drift`}</small></div></article>)}
          {!loading && !error && !rows.length && <div className="empty-state"><Icon name="clock" /><h3>No attendance matches these filters.</h3></div>}
        </div>
        {nextCursor && <div className="load-more"><button className="button secondary" disabled={loading} onClick={() => void load(nextCursor, true)}>{loading ? 'Loading…' : 'Load older events'}</button><small>{rows.length.toLocaleString()} events loaded</small></div>}
      </section>
    </>
  )
}

export function AlertsView({ devices, toast, revision }: { devices: Device[]; toast: ReturnType<typeof useToast>; revision: number }) {
  const [rows, setRows] = useState<AlertQueueResponse['rows']>([])
  const [queue, setQueue] = useState<'OPEN' | 'ACKNOWLEDGED' | 'RESOLVED' | 'ALL'>('OPEN')
  const [severity, setSeverity] = useState('ALL')
  const [deviceId, setDeviceId] = useState('ALL')
  const [totals, setTotals] = useState({ all: 0, open: 0, acknowledged: 0, resolved: 0 })
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const requestRef = useRef<AbortController | null>(null)
  const load = useCallback(async (cursor?: string, append = false) => {
    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    setLoading(true)
    setError('')
    try {
      const response = await api<AlertQueueResponse>(`/api/v1/alerts${queryString({
        state: queue === 'ALL' ? undefined : queue,
        severity: severity === 'ALL' ? undefined : severity,
        connector_id: deviceId === 'ALL' ? undefined : deviceId,
        cursor,
        limit: 100,
      })}`, { signal: controller.signal })
      setRows((current) => append ? [...current, ...response.rows] : response.rows)
      setTotals(response.totals)
      setNextCursor(response.next_cursor)
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setError(reason instanceof Error ? reason.message : 'Unable to load the alert queue.')
    } finally {
      if (requestRef.current === controller) setLoading(false)
    }
  }, [deviceId, queue, severity])
  useEffect(() => {
    void load()
    return () => requestRef.current?.abort()
  }, [load, revision])
  const acknowledge = async (row: Alert) => {
    try {
      await api(`/api/v1/alerts/${row.id}/acknowledge`, { method: 'POST', body: '{}' })
      toast.notice('Alert acknowledged with an audit entry.')
      await load()
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : 'Unable to acknowledge alert.')
    }
  }
  return (
    <>
      <PageHeader eyebrow="OPERATIONS QUEUE" title="Alerts and exceptions" description="Triage current device conditions before reviewing acknowledged and resolved history." action={<button className="button secondary" onClick={() => void load()}><Icon name="refresh" /> Refresh</button>} />
      <section className="queue-toolbar" aria-label="Alert filters">
        <div className="segmented-control" role="group" aria-label="Alert queue">
          <button className={queue === 'OPEN' ? 'active' : ''} onClick={() => setQueue('OPEN')}>Open <span>{totals.open}</span></button>
          <button className={queue === 'ACKNOWLEDGED' ? 'active' : ''} onClick={() => setQueue('ACKNOWLEDGED')}>Acknowledged <span>{totals.acknowledged}</span></button>
          <button className={queue === 'RESOLVED' ? 'active' : ''} onClick={() => setQueue('RESOLVED')}>Resolved <span>{totals.resolved}</span></button>
          <button className={queue === 'ALL' ? 'active' : ''} onClick={() => setQueue('ALL')}>All <span>{totals.all}</span></button>
        </div>
        <div className="queue-selects"><label><span>Severity</span><select value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="ALL">All severities</option><option value="CRITICAL">Critical</option><option value="HIGH">High</option><option value="WARNING">Warning</option></select></label><label><span>Device</span><select value={deviceId} onChange={(event) => setDeviceId(event.target.value)}><option value="ALL">All devices</option>{devices.map((device) => <option key={device.connector_id} value={device.connector_id}>{device.display_name}</option>)}</select></label></div>
      </section>
      <section className="alert-list">
        {error && <div className="panel message pattern-blocked operational-error" role="alert"><Icon name="alert" /><span>{error}</span><button className="button secondary" onClick={() => void load()}>Retry queue</button></div>}
        {loading && !rows.length && <div className="panel empty-state"><Icon name="refresh" /><h2>Loading the national alert queue…</h2></div>}
        {rows.map((row) => {
          const diagnostics = formatAlertDiagnostics(row.details)
          const inspectorPath = typeof row.details.inspector_path === 'string' && /^\/reconciliation\?tab=source-exceptions&device_id=[A-Za-z0-9-]{1,100}$/.test(row.details.inspector_path) ? row.details.inspector_path : null
          return <article className={`alert-card pattern-${statusPattern(row.severity)}`} key={`${row.device.connector_id}-${row.id}`}><span className="alert-icon"><Icon name="alert" /></span><div><div className="alert-meta"><StatusBadge state={row.severity} /><a href={routePath('fleet', row.device.connector_id)}>{row.device.display_name} · {row.device.zone_id}</a></div><h2>{row.message}</h2><p>{row.code} · First {dateTime(row.first_seen_at)} · Last {relativeTime(row.last_seen_at)}</p>{diagnostics && <p className="alert-diagnostics" aria-label="Safe alert diagnostics">{diagnostics}</p>}</div><div className="alert-actions">{inspectorPath && <a className="button primary" href={inspectorPath}><Icon name="search" /> Inspect source rows</a>}{row.state === 'OPEN' ? <button className="button secondary" onClick={() => void acknowledge(row)}><Icon name="check" /> Acknowledge</button> : <StatusBadge state={row.state} />}</div></article>
        })}
        {!loading && !error && !rows.length && <div className="panel empty-state"><Icon name="shield" /><h2>No alerts in this view.</h2><p>Change the queue filters or wait for the next live telemetry update.</p></div>}
        {nextCursor && <div className="load-more"><button className="button secondary" disabled={loading} onClick={() => void load(nextCursor, true)}>{loading ? 'Loading…' : 'Load more alerts'}</button><small>{rows.length.toLocaleString()} alerts loaded</small></div>}
      </section>
    </>
  )
}
