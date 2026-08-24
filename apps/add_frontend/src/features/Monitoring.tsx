import { useCallback, useEffect, useRef, useState } from 'react'
import { api, queryString } from '../api'
import {
  Dialog, PageHeader, StatusBadge, dateTime, formatAlertDiagnostics, idempotency,
  relativeTime, statusPattern, useToast,
} from '../App'
import { Icon } from '../Icon'
import { routePath } from '../routing'
import type {
  Alert, AlertQueueResponse, AttendanceQuarantineItem,
  AttendanceQuarantineResponse, AttendanceQuarantineReveal, Device,
} from '../types'

function AttendanceQuarantineDialog({
  row,
  onClose,
  onChanged,
  toast,
}: {
  row: AttendanceQuarantineItem
  onClose: () => void
  onChanged: () => Promise<void>
  toast: ReturnType<typeof useToast>
}) {
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [revealed, setRevealed] = useState<AttendanceQuarantineReveal | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    if (!revealed) return
    const hide = () => setRevealed(null)
    const timeout = window.setTimeout(hide, 60_000)
    const hideOnBackground = () => {
      if (document.hidden) hide()
    }
    document.addEventListener('visibilitychange', hideOnBackground)
    return () => {
      window.clearTimeout(timeout)
      document.removeEventListener('visibilitychange', hideOnBackground)
    }
  }, [revealed])
  useEffect(() => () => setRevealed(null), [])
  const act = async (action: 'review' | 'reveal') => {
    setBusy(true)
    setError('')
    try {
      const body = JSON.stringify({
        reason: reason.trim(),
        password,
        idempotency_key: idempotency(`attendance-quarantine-${action}`),
      })
      if (action === 'review') {
        await api(`/api/v1/attendance-quarantine/${row.id}/review`, {
          method: 'POST',
          body,
        })
        setPassword('')
        setReason('')
        setRevealed(null)
        toast.notice('Attendance quarantine review recorded with an audit entry.')
        await onChanged()
        onClose()
      } else {
        setRevealed(await api<AttendanceQuarantineReveal>(
          `/api/v1/attendance-quarantine/${row.id}/reveal`,
          { method: 'POST', body },
        ))
        setPassword('')
        setReason('')
        toast.notice('Protected attendance evidence revealed with an audit entry.')
      }
    } catch (reason) {
      setPassword('')
      setError(reason instanceof Error ? reason.message : 'Attendance quarantine action failed.')
    } finally {
      setBusy(false)
    }
  }
  const canAct = reason.trim().length >= 10 && Boolean(password) && !busy
  return (
    <Dialog
      titleId="attendance-quarantine-review-title"
      title="Review quarantined attendance"
      description={`${row.display_name} · batch item ${row.item_index}`}
      onClose={onClose}
      className="device-drawer attendance-quarantine-drawer"
    >
      <div className="drawer-status">
        <StatusBadge state="QUARANTINED" />
        <span>Valid and newer punches were settled independently.</span>
      </div>
      <div className="drawer-content quarantine-review-content">
        <article className="info-copy pattern-waiting">
          <Icon name="shield" />
          <div>
            <h3>Review does not create or replay attendance.</h3>
            <p>It records an audited disposition for retained evidence. Protected payload data is shown only after a separate step-up action and hides automatically.</p>
          </div>
        </article>
        <dl className="reconciliation-detail-facts">
          <div><dt>Validation code</dt><dd>{row.error_code || 'VALIDATION_ERROR'}</dd></div>
          <div><dt>Field path</dt><dd>{row.error_path || 'Batch-level'}</dd></div>
          <div><dt>Receipt</dt><dd>{row.receipt_id}</dd></div>
          <div><dt>Evidence digest</dt><dd className="mono-value">{row.payload_digest}</dd></div>
        </dl>
        <label>
          Audited reason
          <textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} placeholder="At least 10 characters" />
        </label>
        <label>
          Administrator password
          <input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        {revealed && (
          <article className="protected-evidence" aria-label="Protected attendance evidence">
            <div><strong>Protected evidence</strong><button className="button secondary" onClick={() => setRevealed(null)}>Hide now</button></div>
            <p>Automatically hidden after 60 seconds or when this tab moves to the background.</p>
            <pre>{JSON.stringify(revealed.payload, null, 2)}</pre>
          </article>
        )}
        <div className="dialog-actions">
          <button className="button secondary" onClick={onClose}>Close</button>
          <button className="button secondary" disabled={!canAct || !row.evidence_available} onClick={() => void act('reveal')}><Icon name="search" /> {busy ? 'Verifying…' : 'Reveal evidence'}</button>
          <button className="button primary" disabled={!canAct} onClick={() => void act('review')}><Icon name="check" /> {busy ? 'Recording…' : 'Mark reviewed'}</button>
        </div>
      </div>
    </Dialog>
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
  const [quarantine, setQuarantine] = useState<AttendanceQuarantineResponse | null>(null)
  const [quarantineError, setQuarantineError] = useState('')
  const [selectedQuarantine, setSelectedQuarantine] = useState<AttendanceQuarantineItem | null>(null)
  const requestRef = useRef<AbortController | null>(null)
  const quarantineRequestRef = useRef<AbortController | null>(null)
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
  const loadQuarantine = useCallback(async () => {
    quarantineRequestRef.current?.abort()
    const controller = new AbortController()
    quarantineRequestRef.current = controller
    setQuarantineError('')
    setQuarantine(null)
    try {
      const response = await api<AttendanceQuarantineResponse>(`/api/v1/attendance-quarantine${queryString({
        device_id: deviceId === 'ALL' ? undefined : deviceId,
        review_state: 'OPEN',
        limit: 10,
      })}`, { signal: controller.signal })
      if (!response?.totals || !Array.isArray(response.rows)) {
        throw new Error('Attendance quarantine diagnostics returned an invalid response.')
      }
      setQuarantine(response)
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setQuarantineError(reason instanceof Error ? reason.message : 'Unable to load attendance quarantine diagnostics.')
    }
  }, [deviceId])
  useEffect(() => {
    void load()
    return () => requestRef.current?.abort()
  }, [load, revision])
  useEffect(() => {
    void loadQuarantine()
    return () => quarantineRequestRef.current?.abort()
  }, [loadQuarantine, revision])
  useEffect(() => setSelectedQuarantine(null), [deviceId])
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
      <section className="panel attendance-quarantine-panel" aria-labelledby="attendance-quarantine-title">
        <div className="panel-header">
          <div><span className="eyebrow">NON-BLOCKING INGESTION</span><h2 id="attendance-quarantine-title">Attendance quarantine</h2><p>Malformed rows are preserved for review after a durable receipt; valid and newer punches continue through the delivery pipeline.</p></div>
          <button className="button secondary" onClick={() => void loadQuarantine()}><Icon name="refresh" /> Refresh</button>
        </div>
        {quarantineError && <div className="message pattern-blocked operational-error" role="alert"><Icon name="alert" /><span>{quarantineError}</span></div>}
        {quarantine && <div className="quarantine-summary"><StatusBadge state={quarantine.totals.open ? 'WARNING' : 'HEALTHY'} /><strong>{quarantine.totals.open.toLocaleString()} open</strong><span>{quarantine.totals.all.toLocaleString()} retained in total</span></div>}
        {quarantine?.rows.map((row) => <article className="quarantine-row" key={row.id}><div><strong>{row.display_name} · {row.zone_id}</strong><p>{row.error_code || 'VALIDATION_ERROR'}{row.error_path ? ` · ${row.error_path}` : ''} · Batch item {row.item_index}</p></div><div className="quarantine-row-actions"><StatusBadge state="QUARANTINED" /><small>{relativeTime(row.observed_at)} · Receipt {row.receipt_id.slice(0, 8)}</small><button className="button secondary" onClick={() => setSelectedQuarantine(row)}><Icon name="search" /> Review</button></div></article>)}
        {quarantine && !quarantine.rows.length && !quarantineError && <div className="empty-state compact"><Icon name="shield" /><p>No attendance rows are waiting for review in this scope.</p></div>}
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
      {selectedQuarantine && <AttendanceQuarantineDialog row={selectedQuarantine} toast={toast} onClose={() => setSelectedQuarantine(null)} onChanged={async () => { await Promise.all([loadQuarantine(), load()]) }} />}
    </>
  )
}
