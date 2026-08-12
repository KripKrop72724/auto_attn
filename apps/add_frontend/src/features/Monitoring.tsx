import { useCallback, useEffect, useRef, useState } from 'react'
import { api, queryString } from '../api'
import {
  PageHeader, StatusBadge, dateTime, formatAlertDiagnostics,
  relativeTime, statusPattern, useToast,
} from '../App'
import { Icon } from '../Icon'
import { routePath } from '../routing'
import type { Alert, AlertQueueResponse, Device } from '../types'

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
