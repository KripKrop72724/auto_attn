import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { z } from 'zod'
import { api, queryString } from '../api'
import { Dialog, PageHeader, StatusBadge, dateTime, idempotency, relativeTime, statusPattern, useToast, type ReconciliationDialogState } from '../App'
import { Icon } from '../Icon'
import type {
  Device, FirmwareCampaign, FirmwareRelease, FirmwareScopePreview, FirmwareSection,
  ReconciliationDivergenceDetail, ReconciliationDivergenceReveal,
  ReconciliationJob, ReconciliationPreflight, ReconciliationScheduler,
  SourceException, SourceExceptionList, SourceExceptionReveal, SourceExceptionTotals,
} from '../types'

const firmwareScopeSchema = z.object({
  scope_token: z.string().min(32),
  expires_at: z.string().min(1),
  release: z.object({ release_id: z.string(), version: z.string(), state: z.string() }),
  zone_id: z.string(),
  counts: z.object({ candidates: z.number().int().nonnegative(), eligible: z.number().int().nonnegative(), excluded: z.number().int().nonnegative(), offline: z.number().int().nonnegative() }),
  eligible: z.array(z.object({ connector_id: z.string(), display_name: z.string(), zone_id: z.string(), hardware_id: z.string(), connected: z.boolean() }).passthrough()),
  excluded: z.array(z.object({ connector_id: z.string(), display_name: z.string(), zone_id: z.string(), hardware_id: z.string(), connected: z.boolean(), reason: z.string() }).passthrough()),
})

const firmwareCampaignCreatedSchema = z.object({
  campaign_id: z.string().min(1), status: z.string(), eligible: z.number().int().nonnegative(), legacy_skipped: z.number().int().nonnegative(),
})

const reconciliationEtaLabel = (job: ReconciliationJob) => {
  if (job.eta.high_seconds != null) return `${Math.ceil(job.eta.high_seconds / 60)} min`
  if (job.eta.unavailable_reason === 'COMPLETED') return 'Complete'
  if (['CANCELLED', 'FAILED', 'INVALIDATED'].includes(job.eta.unavailable_reason || '')) return 'Stopped'
  if (job.eta.unavailable_reason === 'ORACLE_TERMINAL_OUTCOME_REQUIRES_REVIEW' || job.eta.unavailable_reason === 'ORACLE_OUTCOME_REVIEW_REQUIRED') return 'Review needed'
  if (job.eta.unavailable_reason === 'ORACLE_PROGRESS_RATE_UNAVAILABLE') return 'Awaiting Oracle'
  if (job.eta.unavailable_reason === 'WAITING_FOR_DEVICE') return 'Device wait'
  return 'Measuring'
}

function SourceExceptionDrawer({
  seed,
  onClose,
  onChanged,
  toast,
}: {
  seed: SourceException
  onClose: () => void
  onChanged: () => Promise<void>
  toast: ReturnType<typeof useToast>
}) {
  const [row, setRow] = useState(seed)
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [revealed, setRevealed] = useState<SourceExceptionReveal | null>(null)
  useEffect(() => {
    if (!revealed) return
    const timeout = window.setTimeout(() => setRevealed(null), 60_000)
    const hideOnBackground = () => { if (document.hidden) setRevealed(null) }
    document.addEventListener('visibilitychange', hideOnBackground)
    return () => { window.clearTimeout(timeout); document.removeEventListener('visibilitychange', hideOnBackground) }
  }, [revealed])
  const [busy, setBusy] = useState(false)
  const refresh = useCallback(async () => {
    setRow(await api<SourceException>(`/api/v1/source-exceptions/${seed.id}`))
  }, [seed.id])
  useEffect(() => { void refresh() }, [refresh])
  const act = async (action: 'review' | 'reveal') => {
    setBusy(true)
    try {
      const body = JSON.stringify({
        reason: reason.trim(),
        password,
        idempotency_key: idempotency(`source-exception-${action}`),
      })
      if (action === 'review') {
        setRow(await api<SourceException>(`/api/v1/source-exceptions/${row.id}/review`, { method: 'POST', body }))
        toast.notice('Source exception marked reviewed without changing terminal truth.')
        await onChanged()
      } else {
        setRevealed(await api<SourceExceptionReveal>(`/api/v1/source-exceptions/${row.id}/reveal`, { method: 'POST', body }))
        toast.notice('Protected source evidence revealed with an audit entry.')
      }
      setPassword('')
      setReason('')
    } catch (error) {
      setPassword('')
      toast.error(error instanceof Error ? error.message : 'Source exception action failed.')
    } finally {
      setBusy(false)
    }
  }
  const canAct = reason.trim().length >= 10 && Boolean(password) && !busy
  return (
    <Dialog titleId="source-exception-title" title="Terminal source exception" description={`${row.zone_id || 'Unknown zone'} · ordinal ${row.ordinal.toLocaleString()}`} onClose={onClose} className="device-drawer source-exception-drawer">
      <div className="drawer-status"><StatusBadge state={row.disposition} /><span>{row.cursor_advanced ? `Source cursor safely advanced to ${row.source_committed_cursor.toLocaleString()}` : 'Source cursor has not advanced beyond this row'}</span></div>
      <div className="drawer-content source-exception-detail">
        <article className="info-copy pattern-blocked"><Icon name="shield" /><div><h3>Excluded from attendance and Oracle</h3><p>ADD preserved this terminal ordinal as immutable evidence. Review does not create, edit, or delete attendance, and valid punches after this row can continue.</p></div></article>
        <dl className="exception-facts">
          <div><dt>Device</dt><dd>{row.display_name || row.connector_id || 'Unknown'}</dd></div>
          <div><dt>Terminal</dt><dd>{row.terminal_serial}</dd></div>
          <div><dt>Generation / ordinal</dt><dd>{row.terminal_generation} / {row.ordinal.toLocaleString()}</dd></div>
          <div><dt>Source</dt><dd>{row.source_kind}</dd></div>
          <div><dt>Error</dt><dd>{row.error_code || row.disposition}</dd></div>
          <div><dt>Raw timestamp</dt><dd>{row.raw_timestamp ?? 'Unavailable'}</dd></div>
          <div><dt>Observed identity</dt><dd>UID {row.observed_uid || '—'} · User {row.observed_user_id || '—'}</dd></div>
          <div><dt>Record size</dt><dd>{row.record_size ?? '—'} bytes</dd></div>
          <div className="wide"><dt>SHA-256 evidence digest</dt><dd><code>{row.raw_record_digest}</code></dd></div>
          <div className="wide"><dt>Terminal record key</dt><dd><code>{row.terminal_record_key}</code></dd></div>
        </dl>
        <section className="exception-review-section">
          <div className="panel-header"><div><h3>Review history</h3><p>Review acknowledges the evidence only; the source row remains immutable and fail-closed.</p></div><StatusBadge state={row.review_state} /></div>
          {(row.reviews || []).map((review) => <article key={review.review_id} className="exception-review"><strong>{review.actor}</strong><span>{dateTime(review.created_at)}</span><p>{review.reason}</p></article>)}
          {!(row.reviews || []).length && <p className="muted-copy">No operator review has been recorded.</p>}
        </section>
        {revealed && <section className="revealed-evidence" aria-live="polite"><div className="panel-header"><div><h3>Protected raw evidence</h3><p>Visible only in this step-up response. It is not cached by ADD.</p></div><button className="button secondary" onClick={() => setRevealed(null)}>Hide</button></div><label>Hex<code>{revealed.raw_record_hex}</code></label><label>Base64<code>{revealed.raw_record_b64}</code></label></section>}
        <section className="exception-actions">
          <label>Audited reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} placeholder="At least 10 characters" /></label>
          <label>Administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          <div className="dialog-actions"><button className="button secondary" disabled={!canAct} onClick={() => void act('reveal')}><Icon name="search" /> Reveal raw evidence</button><button className="button primary" disabled={!canAct || row.review_state === 'REVIEWED'} onClick={() => void act('review')}><Icon name="check" /> Mark reviewed</button></div>
        </section>
      </div>
    </Dialog>
  )
}

function ReconciliationDivergenceDrawer({
  divergenceId,
  onClose,
  toast,
}: {
  divergenceId: string
  onClose: () => void
  toast: ReturnType<typeof useToast>
}) {
  const [row, setRow] = useState<ReconciliationDivergenceDetail | null>(null)
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [revealed, setRevealed] = useState<ReconciliationDivergenceReveal | null>(null)
  useEffect(() => {
    if (!revealed) return
    const timeout = window.setTimeout(() => setRevealed(null), 60_000)
    const hideOnBackground = () => { if (document.hidden) setRevealed(null) }
    document.addEventListener('visibilitychange', hideOnBackground)
    return () => { window.clearTimeout(timeout); document.removeEventListener('visibilitychange', hideOnBackground) }
  }, [revealed])
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    void api<ReconciliationDivergenceDetail>(`/api/v1/reconciliation-divergences/${divergenceId}`)
      .then(setRow)
      .catch((error) => toast.error(error instanceof Error ? error.message : 'Could not load source-change evidence.'))
  }, [divergenceId, toast])
  const reveal = async () => {
    setBusy(true)
    try {
      setRevealed(await api<ReconciliationDivergenceReveal>(`/api/v1/reconciliation-divergences/${divergenceId}/reveal`, {
        method: 'POST',
        body: JSON.stringify({
          reason: reason.trim(),
          password,
          idempotency_key: idempotency('source-divergence-reveal'),
        }),
      }))
      setPassword('')
      setReason('')
      toast.notice('Protected source-change evidence revealed with an audit entry.')
    } catch (error) {
      setPassword('')
      toast.error(error instanceof Error ? error.message : 'Could not reveal source-change evidence.')
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog titleId="source-divergence-title" title="Terminal-history change" description={row ? `Ordinal ${row.ordinal.toLocaleString()} · ${row.state.replaceAll('_', ' ').toLowerCase()}` : 'Loading immutable evidence'} onClose={onClose} className="device-drawer source-exception-drawer">
      <div className="drawer-status"><StatusBadge state={row?.state || 'LOADING'} /><span>Old evidence remains immutable; recovery never overwrites Oracle.</span></div>
      <div className="drawer-content source-exception-detail">
        {row && <>
          <article className="info-copy pattern-waiting"><Icon name="shield" /><div><h3>Independent fresh-buffer verification</h3><p>ADD compares raw bytes, not parser labels. A stable change creates a preserved source epoch; a transient read resumes from the existing checkpoint.</p></div></article>
          <dl className="exception-facts">
            <div><dt>Job</dt><dd>{row.job_id || 'Unknown'}</dd></div><div><dt>Ordinal</dt><dd>{row.ordinal.toLocaleString()}</dd></div>
            <div><dt>Original disposition</dt><dd>{row.old_disposition || 'Unknown'}</dd></div><div><dt>Observed disposition</dt><dd>{row.new_disposition || 'Unknown'}</dd></div>
            <div className="wide"><dt>Original SHA-256</dt><dd><code>{row.old_raw_digest}</code></dd></div><div className="wide"><dt>Observed SHA-256</dt><dd><code>{row.new_raw_digest}</code></dd></div>
          </dl>
          <section className="exception-review-section"><div className="panel-header"><div><h3>Observation timeline</h3><p>Each probe comes from an independently prepared terminal buffer.</p></div><StatusBadge state={`${row.observations.length} OBSERVATIONS`} /></div>{row.observations.map((item, index) => <article className="exception-review" key={`${item.observed_at}-${index}`}><strong>{item.kind.replaceAll('_', ' ')}</strong><span>{dateTime(item.observed_at)}</span><p>{item.raw_record_digest}</p></article>)}</section>
          {revealed && <section className="revealed-evidence" aria-live="polite"><div className="panel-header"><div><h3>Protected raw evidence</h3><p>This no-store response is audited and never written to application logs.</p></div><button className="button secondary" onClick={() => setRevealed(null)}>Hide</button></div><label>Hex<code>{revealed.raw_record_hex}</code></label><label>Base64<code>{revealed.raw_record_b64}</code></label></section>}
          {row.evidence_available && <section className="exception-actions"><label>Audited reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} placeholder="At least 10 characters" /></label><label>Administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label><div className="dialog-actions"><button className="button primary" disabled={reason.trim().length < 10 || !password || busy} onClick={() => void reveal()}><Icon name="search" /> {busy ? 'Verifying…' : 'Reveal raw evidence'}</button></div></section>}
        </>}
      </div>
    </Dialog>
  )
}

export function ReconciliationView({
  devices,
  revision,
  toast,
}: {
  devices: Device[]
  revision: number
  toast: ReturnType<typeof useToast>
}) {
  const initialSourceFilters = new URLSearchParams(window.location.search)
  const [rows, setRows] = useState<ReconciliationJob[]>([])
  const [scheduler, setScheduler] = useState<ReconciliationScheduler>({
    policy: 'BOUNDED_PARALLEL_PER_DEVICE',
    device_concurrency: 1,
    active_scan_jobs: 0,
    waiting_scan_jobs: 0,
    available_scan_slots: 1,
  })
  const [enabled, setEnabled] = useState(false)
  const [selectedId, setSelectedId] = useState('')
  const [preflight, setPreflight] = useState<ReconciliationPreflight | null>(null)
  const [dialog, setDialog] = useState<ReconciliationDialogState>(null)
  const [divergenceDrawerId, setDivergenceDrawerId] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [section, setSection] = useState<'jobs' | 'exceptions'>(
    initialSourceFilters.get('tab') === 'source-exceptions' ? 'exceptions' : 'jobs',
  )
  const [exceptionRows, setExceptionRows] = useState<SourceException[]>([])
  const [exceptionTotals, setExceptionTotals] = useState<SourceExceptionTotals>({ all: 0, open: 0, reviewed: 0, invalid_time: 0, malformed: 0, affected_terminals: 0 })
  const [exceptionCursor, setExceptionCursor] = useState<number | null>(null)
  const [exceptionLoading, setExceptionLoading] = useState(false)
  const [exceptionDrawer, setExceptionDrawer] = useState<SourceException | null>(null)
  const [exceptionFilters, setExceptionFilters] = useState({
    device_id: initialSourceFilters.get('device_id') || '',
    disposition: '',
    error_code: '',
    review_state: '',
    ordinal: '',
  })
  const selected = devices.find((device) => device.connector_id === selectedId) || null

  const load = useCallback(async () => {
    try {
      const response = await api<{
        enabled: boolean
        scheduler?: ReconciliationScheduler
        rows: ReconciliationJob[]
      }>(
        '/api/v1/reconciliations?limit=100',
      )
      setEnabled(response.enabled)
      setRows(response.rows)
      if (response.scheduler) setScheduler(response.scheduler)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Unable to read reconciliation jobs.')
    }
  }, [toast.error])

  const loadExceptions = useCallback(async (cursor?: number, append = false) => {
    setExceptionLoading(true)
    try {
      const response = await api<SourceExceptionList>(`/api/v1/source-exceptions${queryString({ ...exceptionFilters, ordinal: exceptionFilters.ordinal || undefined, cursor, limit: 100 })}`)
      setExceptionRows((current) => append ? [...current, ...response.rows] : response.rows)
      setExceptionTotals(response.totals)
      setExceptionCursor(response.next_cursor)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Unable to read terminal source exceptions.')
    } finally {
      setExceptionLoading(false)
    }
  }, [exceptionFilters, toast.error])

  useEffect(() => { void load() }, [load, revision])
  useEffect(() => { void loadExceptions(undefined, false) }, [loadExceptions, revision])
  useEffect(() => {
    setPreflight(null)
    if (!selectedId) {
      return
    }
    const controller = new AbortController()
    api<ReconciliationPreflight>(`/api/v1/devices/${selectedId}/reconciliations/preflight`, { signal: controller.signal })
      .then((response) => { if (!controller.signal.aborted) setPreflight(response) })
      .catch((error) => {
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          toast.error(error instanceof Error ? error.message : 'Preflight failed.')
        }
      })
    return () => controller.abort()
  }, [selectedId, revision, toast.error])

  const closeDialog = () => {
    setDialog(null)
    setReason('')
    setConfirmation('')
    setPassword('')
  }

  const start = async () => {
    if (!selected) return
    setBusy(true)
    try {
      await api(`/api/v1/devices/${selected.connector_id}/reconciliations/full-history`, {
        method: 'POST',
        body: JSON.stringify({
          reason: reason.trim(),
          confirmation,
          password,
          idempotency_key: idempotency('full-history'),
        }),
      })
      closeDialog()
      toast.notice('Durable start-of-time reconciliation queued. ADD now owns its checkpoint.')
      await load()
    } catch (error) {
      setPassword('')
      toast.error(error instanceof Error ? error.message : 'Could not start reconciliation.')
    } finally {
      setBusy(false)
    }
  }

  const control = async (job: ReconciliationJob, action: 'pause' | 'resume' | 'cancel' | 'retry') => {
    setBusy(true)
    try {
      await api(`/api/v1/reconciliations/${job.job_id}/${action}`, {
        method: 'POST',
        body: JSON.stringify({
          reason: reason.trim(),
          password,
          idempotency_key: idempotency(`reconcile-${action}`),
        }),
      })
      closeDialog()
      toast.notice(`Reconciliation ${action} recorded in the audit trail.`)
      await load()
    } catch (error) {
      setPassword('')
      toast.error(error instanceof Error ? error.message : 'Reconciliation control failed.')
    } finally {
      setBusy(false)
    }
  }

  const active = rows.filter((job) => !['COMPLETED', 'FAILED', 'CANCELLED', 'INVALIDATED'].includes(job.status)).length
  const covered = rows.filter((job) => Boolean(job.capture_certified_at)).length
  const pendingOracle = rows.reduce((total, job) => total + job.progress.oracle_pending, 0)
  const reviewOracle = rows.reduce((total, job) => total + (job.progress.oracle_review_required ?? 0), 0)
  const canStart = Boolean(
    selected && enabled && preflight?.eligible && reason.trim().length >= 10 && password &&
    confirmation === `RECONCILE ${selected.device_id} FROM START`,
  )
  const scanQueue = rows
    .filter((job) => !job.capture_certified_at && ['QUEUED', 'RUNNING'].includes(job.status))
    .sort((left, right) => left.requested_at.localeCompare(right.requested_at))
  const queuePositions = new Map(scanQueue.map((job, index) => [job.job_id, index + 1]))

  return (
    <>
      <header className="page-header">
        <div>
          <p className="eyebrow">ADD-OWNED SOURCE ASSURANCE</p>
          <h1>{section === 'jobs' ? 'Start-of-time reconciliation' : 'Terminal source exceptions'}</h1>
          <p>{section === 'jobs' ? 'Request a complete, resumable read of terminal truth. ADD commits every bounded chunk before the device advances and separately proves Oracle membership without deleting Oracle records.' : 'Inspect invalid or malformed terminal rows preserved as immutable evidence without blocking valid punches that follow.'}</p>
        </div>
        {section === 'jobs' ? <button className="button primary" disabled={!selected || !preflight?.eligible || !enabled} onClick={() => setDialog({ mode: 'start' })}><Icon name="refresh" /> New complete reconcile</button> : <button className="button secondary" onClick={() => void loadExceptions(undefined, false)}><Icon name="refresh" /> Refresh exceptions</button>}
      </header>
      <div className="segmented-control reconciliation-tabs" role="tablist" aria-label="Reconciliation workspace"><button role="tab" aria-selected={section === 'jobs'} className={section === 'jobs' ? 'active' : ''} onClick={() => setSection('jobs')}>Jobs <span>{active}</span></button><button role="tab" aria-selected={section === 'exceptions'} className={section === 'exceptions' ? 'active' : ''} onClick={() => setSection('exceptions')}>Source exceptions <span>{exceptionTotals.open}</span></button></div>
      {section === 'jobs' ? <section className="metric-grid">
        <article className="metric-card"><span className="metric-icon"><Icon name="refresh" /></span><div><p>Active jobs</p><strong>{active}</strong><small>{scheduler.device_concurrency} isolated terminal scan slots</small></div></article>
        <article className="metric-card metric-positive"><span className="metric-icon"><Icon name="shield" /></span><div><p>Source certificates</p><strong>{covered}</strong><small>Immutable terminal coverage</small></div></article>
        <article className={`metric-card ${reviewOracle ? 'metric-warning' : ''}`}><span className="metric-icon"><Icon name="server" /></span><div><p>Oracle pending</p><strong>{pendingOracle.toLocaleString()}</strong><small>{reviewOracle ? `${reviewOracle.toLocaleString()} terminal outcomes require review` : 'Append-only membership checks'}</small></div></article>
        <article className={`metric-card ${enabled ? 'metric-positive' : 'metric-warning'}`}><span className="metric-icon"><Icon name="power" /></span><div><p>Production gate</p><strong>{enabled ? 'Enabled' : 'Dark'}</strong><small>{enabled ? `${(scheduler.available_credit ?? 0).toLocaleString()} source credits available · ${(scheduler.history_backlog ?? 0).toLocaleString()} Oracle history queued` : 'Awaiting controlled enablement'}</small></div></article>
      </section> : <section className="metric-grid"><article className="metric-card metric-warning"><span className="metric-icon"><Icon name="alert" /></span><div><p>Open exceptions</p><strong>{exceptionTotals.open.toLocaleString()}</strong><small>Awaiting operator review</small></div></article><article className="metric-card"><span className="metric-icon"><Icon name="clock" /></span><div><p>Invalid timestamps</p><strong>{exceptionTotals.invalid_time.toLocaleString()}</strong><small>Excluded fail-closed</small></div></article><article className="metric-card"><span className="metric-icon"><Icon name="terminal" /></span><div><p>Malformed rows</p><strong>{exceptionTotals.malformed.toLocaleString()}</strong><small>Raw evidence preserved</small></div></article><article className="metric-card"><span className="metric-icon"><Icon name="server" /></span><div><p>Affected terminals</p><strong>{exceptionTotals.affected_terminals.toLocaleString()}</strong><small>Subsequent valid punches continue</small></div></article></section>}
      {section === 'jobs' && <>
      <section className="panel selection-panel">
        <label>Terminal to reconcile<select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}><option value="">Select a device</option>{devices.map((device) => <option key={device.connector_id} value={device.connector_id}>{device.display_name} · {device.zone_id}</option>)}</select></label>
        <div className="selected-device-summary">
          {selected && <><StatusBadge state={selected.connected ? 'ESP ONLINE' : 'ESP OFFLINE'} /><StatusBadge state={selected.zkt?.connection_state || 'NO ZKT'} /><span>{selected.zkt?.attendance_count?.toLocaleString() || '—'} terminal records</span></>}
        </div>
      </section>
      {selected && preflight && (
        <article className={`capability-banner pattern-${preflight.eligible ? preflight.ready_now ? 'confirmed' : 'waiting' : 'blocked'}`}>
          <Icon name={preflight.eligible ? 'shield' : 'alert'} />
          <div><strong>{preflight.ready_now ? 'Preflight ready' : preflight.eligible ? 'Eligible; waiting for a safe window' : 'Preflight blocked'}</strong><span>{[...preflight.hard_blockers, ...preflight.waitable_blockers].map((row) => row.message).join(' ') || 'Signed range-resume firmware, stable identity snapshot, and terminal connectivity are verified.'}</span></div>
          <StatusBadge state={preflight.ready_now ? 'READY' : preflight.eligible ? 'WAITING' : 'BLOCKED'} />
        </article>
      )}
      <section className="panel reconcile-panel">
        <div className="panel-header"><div><h2>Durable reconciliation ledger</h2><p>Progress is ADD-committed; disconnects and restarts resume from the last acknowledged ordinal.</p></div><button className="button secondary" onClick={() => void load()}><Icon name="refresh" /> Refresh</button></div>
        <div className="reconcile-list">
          {rows.map((job) => {
            const cutoff = job.terminal.cutoff_count || 0
            const percent = cutoff ? Math.min(100, Math.round((job.progress.scanned / cutoff) * 100)) : 0
            const queuePosition = queuePositions.get(job.job_id) || 0
            const creditRemaining = job.assignment?.credit_end_ordinal == null
              ? null
              : Math.max(0, job.assignment.credit_end_ordinal - job.progress.scanned)
            const queueStatus = job.operator_message || (job.wait_reason === 'WAITING_FOR_SCAN_SLOT'
              ? `Queue position ${queuePosition}; waiting for one of ${scheduler.device_concurrency} isolated scan slots`
              : job.wait_reason?.replaceAll('_', ' ')
                || `${job.progress.scanned.toLocaleString()} of ${cutoff ? cutoff.toLocaleString() : 'scope pending'} source rows committed`)
            const controls: Array<'pause' | 'resume' | 'cancel' | 'retry'> = []
            if (['QUEUED', 'RUNNING', 'PAUSE_REQUESTED'].includes(job.status)) controls.push('pause')
            if (job.status === 'PAUSED') controls.push('resume')
            if (job.status === 'NEEDS_ATTENTION') controls.push('retry')
            if (!['COMPLETED', 'FAILED', 'CANCELLED', 'INVALIDATED'].includes(job.status)) controls.push('cancel')
            return <article key={job.job_id} className="reconcile-job">
              <div className="reconcile-job-head"><div><p className="eyebrow">{job.connector?.zone_id || 'UNKNOWN ZONE'}</p><h3>{job.connector?.display_name || job.job_id}</h3><small>{job.job_id} · requested {dateTime(job.requested_at)}</small></div><StatusBadge state={job.operator_state || job.status} live={job.status === 'RUNNING'} /></div>
              <div className="reconcile-phase"><strong>{(job.operator_state || job.phase).replaceAll('_', ' ')}</strong><span>{queueStatus}</span></div>
              <div className="reconcile-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><i style={{ width: `${percent}%` }} /></div>
              <div className="reconcile-facts"><span><strong>{percent}%</strong> source scan</span><span><strong>{job.progress.add_durable.toLocaleString()}</strong> ADD durable</span><span><strong>{job.progress.oracle_confirmed.toLocaleString()}</strong> Oracle proven</span><span><strong>{job.progress.blocked_identity.toLocaleString()}</strong> identity held</span><span><strong>{(job.progress.oracle_review_required ?? 0).toLocaleString()}</strong> Oracle review</span><span><strong>{job.progress.quarantined.toLocaleString()}</strong> source quarantined</span><span><strong>{creditRemaining == null ? 'Legacy' : `${creditRemaining.toLocaleString()} rows`}</strong> burst credit</span><span><strong>{reconciliationEtaLabel(job)}</strong> ETA</span><span><strong>{job.recovery?.source_epoch ?? 1}</strong> source epoch</span><span><strong>{job.progress.auto_retry_count ?? 0}</strong> automatic recoveries</span></div>
              {job.recovery?.divergence && <div className="info-copy pattern-waiting"><Icon name="shield" /><div><h3>Terminal source verification at ordinal {job.recovery.divergence.ordinal.toLocaleString()}</h3><p>{job.recovery.divergence.observation_count} independent observation(s) preserved. ADD will resume automatically when the evidence converges.</p></div></div>}
              {job.review_required && <div className="info-copy pattern-waiting"><Icon name="alert" /><div><h3>Current truth certified with historical review</h3><p>ADD preserved a terminal-history change. Oracle remains append-only and no evidence was removed.</p></div></div>}
              {job.error_message && <p className="reconcile-error"><Icon name="alert" />{job.error_message}</p>}
              <div className="reconcile-actions"><a className="button text-button" href={`/api/v1/reconciliations/${job.job_id}/evidence`} target="_blank" rel="noreferrer"><Icon name="shield" /> Evidence</a>{controls.map((action) => <button key={action} className={`button ${action === 'cancel' ? 'destructive' : 'secondary'}`} onClick={() => setDialog({ mode: 'control', job, action })}>{action}</button>)}</div>
            </article>
          })}
          {!rows.length && <div className="empty-state"><Icon name="refresh" /><h3>No complete reconciliation has been requested.</h3><p>Select an eligible terminal, review preflight, and create the first durable source-coverage job.</p></div>}
        </div>
      </section>
      </>}
      {section === 'exceptions' && <section className="panel source-exceptions-panel">
        <div className="panel-header"><div><h2>Immutable source exception ledger</h2><p>Every row remains tied to its terminal generation and ordinal. Review never changes attendance or Oracle.</p></div><StatusBadge state={`${exceptionTotals.all} ACCOUNTED`} /></div>
        {rows.some((job) => Boolean(job.recovery?.divergence)) && <div className="info-copy pattern-waiting"><Icon name="shield" /><div><h3>Preserved terminal-history changes</h3>{rows.filter((job) => job.recovery?.divergence).map((job) => <p key={job.job_id}><button className="button text-button" onClick={() => setDivergenceDrawerId(job.recovery?.divergence?.divergence_id || null)}><strong>{job.connector?.display_name || job.job_id}</strong> · ordinal {job.recovery?.divergence?.ordinal.toLocaleString()} · {job.recovery?.divergence?.state.replaceAll('_', ' ').toLowerCase()} · {job.recovery?.divergence?.observation_count} observation(s)</button></p>)}</div></div>}
        <div className="filter-grid source-exception-filters">
          <label>Device<select value={exceptionFilters.device_id} onChange={(event) => setExceptionFilters({ ...exceptionFilters, device_id: event.target.value })}><option value="">All devices</option>{devices.map((device) => <option key={device.connector_id} value={device.connector_id}>{device.display_name}</option>)}</select></label>
          <label>Disposition<select value={exceptionFilters.disposition} onChange={(event) => setExceptionFilters({ ...exceptionFilters, disposition: event.target.value })}><option value="">All exceptions</option><option value="INVALID_TIME">Invalid timestamp</option><option value="MALFORMED">Malformed record</option></select></label>
          <label>Review state<select value={exceptionFilters.review_state} onChange={(event) => setExceptionFilters({ ...exceptionFilters, review_state: event.target.value })}><option value="">Open and reviewed</option><option value="OPEN">Open</option><option value="REVIEWED">Reviewed</option></select></label>
          <label>Error code<input value={exceptionFilters.error_code} onChange={(event) => setExceptionFilters({ ...exceptionFilters, error_code: event.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, '').slice(0, 120) })} placeholder="IMPLAUSIBLE_TERMINAL_TIME" /></label>
          <label>Exact ordinal<input inputMode="numeric" value={exceptionFilters.ordinal} onChange={(event) => setExceptionFilters({ ...exceptionFilters, ordinal: event.target.value.replace(/\D/g, '').slice(0, 10) })} /></label>
        </div>
        <div className="source-exception-table" aria-label="Terminal source exceptions">
          <div className="source-exception-head" aria-hidden="true"><span>Terminal source</span><span>Exception</span><span>Observed evidence</span><span>Assurance</span><span>Review</span></div>
          {exceptionLoading && !exceptionRows.length && <div className="empty-state compact">Loading source evidence…</div>}
          {exceptionRows.map((row) => <button type="button" className="source-exception-row" aria-label={`Inspect source exception ordinal ${row.ordinal} on ${row.display_name || row.terminal_serial}`} key={row.id} onClick={() => setExceptionDrawer(row)}><span><strong>{row.display_name || row.terminal_serial}</strong><small>{row.zone_id || 'Unknown zone'} · generation {row.terminal_generation} · ordinal {row.ordinal.toLocaleString()}</small></span><span><StatusBadge state={row.disposition} /><small>{row.error_code || 'No error code'}</small></span><span><strong>{row.raw_timestamp ?? 'No valid timestamp'}</strong><small>UID {row.observed_uid || '—'} · User {row.observed_user_id || '—'}</small></span><span><StatusBadge state={row.cursor_advanced ? 'CURSOR ADVANCED' : 'HELD'} /><small>{row.cursor_advanced ? `Committed through ${row.source_committed_cursor.toLocaleString()}` : 'Awaiting durable commit'}</small></span><span><StatusBadge state={row.review_state} /><small>{row.reviewed_at ? dateTime(row.reviewed_at) : 'Open for review'}</small></span></button>)}
          {!exceptionLoading && !exceptionRows.length && <div className="empty-state"><Icon name="shield" /><h3>No source exceptions match these filters.</h3><p>Valid terminal rows remain in the attendance ledger; only fail-closed evidence appears here.</p></div>}
        </div>
        {exceptionCursor && <div className="load-more"><button className="button secondary" disabled={exceptionLoading} onClick={() => void loadExceptions(exceptionCursor, true)}>{exceptionLoading ? 'Loading…' : 'Load older exceptions'}</button><small>{exceptionRows.length.toLocaleString()} exceptions loaded</small></div>}
      </section>}
      {dialog && (
        <Dialog titleId="reconciliation-dialog-title" title={dialog.mode === 'start' ? 'Start complete terminal reconciliation' : `${dialog.action} reconciliation`} description={dialog.mode === 'start' ? selected?.display_name : dialog.job.connector?.display_name} onClose={closeDialog}>
          <div className="dialog-body">
            <div className={`info-copy pattern-${dialog.mode === 'start' || dialog.action === 'cancel' ? 'blocked' : 'waiting'}`}><Icon name="shield" /><div><h3>{dialog.mode === 'start' ? `ADD runs up to ${scheduler.device_concurrency} isolated terminal scans in parallel.` : 'ADD preserves all committed evidence and checkpoints.'}</h3><p>Each device remains strictly serial. Live punches keep priority, and only the affected job pauses for commands, leases, or disconnects; global Oracle backpressure safely pauses source intake.</p></div></div>
            <label>Audited reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} placeholder="At least 10 characters" /></label>
            {dialog.mode === 'start' && selected && <label>Type <code>{`RECONCILE ${selected.device_id} FROM START`}</code><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label>}
            <label>Administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
            <div className="dialog-actions"><button className="button secondary" onClick={closeDialog}>Keep current state</button>{dialog.mode === 'start' ? <button className="button primary" disabled={!canStart || busy} onClick={() => void start()}>{busy ? 'Verifying…' : 'Queue durable reconcile'}</button> : <button className={dialog.action === 'cancel' ? 'button destructive' : 'button primary'} disabled={reason.trim().length < 10 || !password || busy} onClick={() => void control(dialog.job, dialog.action)}>{busy ? 'Verifying…' : `Confirm ${dialog.action}`}</button>}</div>
          </div>
        </Dialog>
      )}
      {exceptionDrawer && <SourceExceptionDrawer seed={exceptionDrawer} onClose={() => setExceptionDrawer(null)} onChanged={() => loadExceptions(undefined, false)} toast={toast} />}
      {divergenceDrawerId && <ReconciliationDivergenceDrawer divergenceId={divergenceDrawerId} onClose={() => setDivergenceDrawerId(null)} toast={toast} />}
    </>
  )
}

type FirmwareListResponse<T> = {
  rows: T[]
  enabled: boolean
  hil_enabled: boolean
}

function FirmwareCampaignControls({
  campaign,
  onChanged,
  toast,
}: {
  campaign: FirmwareCampaign
  onChanged: () => Promise<void>
  toast: ReturnType<typeof useToast>
}) {
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [pendingAction, setPendingAction] = useState<'pause' | 'resume' | 'cancel' | null>(null)
  const actionAllowed = reason.trim().length >= 10 && Boolean(password) && !busy

  const control = async (action: 'pause' | 'resume' | 'cancel') => {
    setBusy(true)
    try {
      await api(`/api/v1/firmware/campaigns/${campaign.campaign_id}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ reason: reason.trim(), password }),
      })
      setPassword('')
      setReason('')
      setPendingAction(null)
      toast.notice(`Firmware campaign ${action === 'pause' ? 'paused' : action === 'resume' ? 'resumed' : 'cancelled'} with an audit entry.`)
      await onChanged()
    } catch (error) {
      setPassword('')
      toast.error(error instanceof Error ? error.message : 'Campaign control failed.')
    } finally {
      setBusy(false)
    }
  }

  if (!['ACTIVE', 'PAUSED'].includes(campaign.status)) return null
  return (
    <div className="firmware-controls">
      <div className="firmware-control-copy"><strong>Campaign controls</strong><small>Every change records an audited reason and administrator step-up.</small></div>
      <div className="firmware-control-actions">
        {campaign.status === 'ACTIVE' && (
          <button className="button secondary" onClick={() => setPendingAction('pause')}>
            <Icon name="pause" /> Pause
          </button>
        )}
        {campaign.status === 'PAUSED' && (
          <button className="button primary" onClick={() => setPendingAction('resume')}>
            <Icon name="refresh" /> Resume
          </button>
        )}
        <button className="button destructive" onClick={() => setPendingAction('cancel')}>
          <Icon name="x" /> Cancel campaign
        </button>
      </div>
      {pendingAction && <Dialog titleId="campaign-control-title" title={`${pendingAction === 'cancel' ? 'Cancel' : pendingAction === 'pause' ? 'Pause' : 'Resume'} firmware campaign`} description={`${campaign.zone_id} · ${campaign.version || 'Unknown release'} · ${campaign.campaign_id}`} onClose={() => { setPendingAction(null); setPassword(''); setReason('') }}>
        <div className="dialog-body">
          <div className={`info-copy pattern-${pendingAction === 'cancel' ? 'blocked' : 'waiting'}`}><Icon name={pendingAction === 'cancel' ? 'alert' : 'info'} /><div><h3>{pendingAction === 'cancel' ? 'This rollout cannot be resumed after cancellation.' : 'The campaign state changes immediately after server verification.'}</h3><p>Devices already applying signed bytes continue according to the durable deployment state.</p></div></div>
          <label>Control reason<input value={reason} onChange={(event) => setReason(event.target.value)} maxLength={200} placeholder="10–200 characters" /></label>
          <label>Administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          <div className="dialog-actions"><button className="button secondary" onClick={() => setPendingAction(null)}>Keep current state</button><button className={`button ${pendingAction === 'cancel' ? 'destructive' : 'primary'}`} disabled={!actionAllowed} onClick={() => void control(pendingAction)}>{busy ? 'Verifying…' : `Confirm ${pendingAction}`}</button></div>
        </div>
      </Dialog>}
    </div>
  )
}

export function FirmwareView({
  devices,
  revision,
  toast,
  section,
  onSection,
}: {
  devices: Device[]
  revision: number
  toast: ReturnType<typeof useToast>
  section: FirmwareSection
  onSection: (section: FirmwareSection) => void
}) {
  const [releases, setReleases] = useState<FirmwareRelease[]>([])
  const [campaigns, setCampaigns] = useState<FirmwareCampaign[]>([])
  const [enabled, setEnabled] = useState(false)
  const [hilEnabled, setHilEnabled] = useState(false)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [releaseId, setReleaseId] = useState('')
  const [zoneId, setZoneId] = useState('')
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [creatorOpen, setCreatorOpen] = useState(false)
  const [scopePreview, setScopePreview] = useState<FirmwareScopePreview | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [revokeRelease, setRevokeRelease] = useState<FirmwareRelease | null>(null)
  const [revokeReason, setRevokeReason] = useState('')
  const [revokePassword, setRevokePassword] = useState('')
  const [revokeBusy, setRevokeBusy] = useState(false)

  const zones = useMemo(
    () => [...new Set(devices.map((device) => device.zone_id).filter(Boolean))].sort(),
    [devices],
  )
  const selectedRelease = releases.find((release) => release.release_id === releaseId) || null
  const hilTargetDevice = selectedRelease?.hil_target_mac
    ? devices.find(
        (device) =>
          device.hardware_id.toLowerCase() === selectedRelease.hil_target_mac?.toLowerCase(),
      ) || null
    : null
  const isHilRelease = selectedRelease?.state === 'HIL_ONLY'
  const selectedZoneMatchesHil = !isHilRelease || Boolean(
    hilTargetDevice && zoneId === hilTargetDevice.zone_id,
  )
  const releaseChannelEnabled = isHilRelease ? hilEnabled : enabled
  const startAllowed = Boolean(
    selectedRelease &&
      selectedRelease.state !== 'REVOKED' &&
      releaseChannelEnabled &&
      selectedZoneMatchesHil &&
      zoneId &&
      reason.trim().length >= 10 &&
      confirmation === selectedRelease.version &&
      password &&
      scopePreview?.release.release_id === selectedRelease.release_id &&
      scopePreview.zone_id === zoneId &&
      !busy,
  )

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [releaseResponse, campaignResponse] = await Promise.all([
        api<FirmwareListResponse<FirmwareRelease>>('/api/v1/firmware/releases'),
        api<FirmwareListResponse<FirmwareCampaign>>('/api/v1/firmware/campaigns'),
      ])
      setReleases(releaseResponse.rows)
      setCampaigns(campaignResponse.rows)
      setEnabled(releaseResponse.enabled)
      setHilEnabled(releaseResponse.hil_enabled)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Unable to load firmware operations.')
    } finally {
      setLoading(false)
    }
  }, [toast.error])

  useEffect(() => { void load() }, [load, revision])

  const selectRelease = (nextReleaseId: string) => {
    setReleaseId(nextReleaseId)
    setConfirmation('')
    setScopePreview(null)
    const nextRelease = releases.find((release) => release.release_id === nextReleaseId)
    if (nextRelease?.state === 'HIL_ONLY' && nextRelease.hil_target_mac) {
      const target = devices.find(
        (device) => device.hardware_id.toLowerCase() === nextRelease.hil_target_mac?.toLowerCase(),
      )
      setZoneId(target?.zone_id || '')
    } else {
      setZoneId('')
    }
  }

  const previewScope = async () => {
    if (!selectedRelease || !zoneId) return
    setPreviewBusy(true)
    setScopePreview(null)
    try {
      const response = await api<unknown>('/api/v1/firmware/campaigns/preflight', {
        method: 'POST',
        body: JSON.stringify({ release_id: selectedRelease.release_id, zone_id: zoneId }),
      })
      setScopePreview(firmwareScopeSchema.parse(response) as FirmwareScopePreview)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Firmware scope could not be previewed.')
    } finally {
      setPreviewBusy(false)
    }
  }

  const start = async (event: FormEvent) => {
    event.preventDefault()
    if (!selectedRelease || !startAllowed) return
    setBusy(true)
    try {
      const result = firmwareCampaignCreatedSchema.parse(await api<unknown>(
        '/api/v1/firmware/campaigns',
        {
          method: 'POST',
          body: JSON.stringify({
            release_id: selectedRelease.release_id,
            zone_id: zoneId,
            reason: reason.trim(),
            typed_confirmation: confirmation,
            password,
            scope_token: scopePreview?.scope_token,
            idempotency_key: idempotency('firmware-campaign'),
          }),
        },
      ))
      setPassword('')
      setConfirmation('')
      setReason('')
      setScopePreview(null)
      toast.notice(`Firmware campaign created for ${result.eligible} eligible device${result.eligible === 1 ? '' : 's'}; ${result.legacy_skipped} skipped.`)
      setCreatorOpen(false)
      await load()
    } catch (error) {
      setPassword('')
      setConfirmation('')
      toast.error(error instanceof Error ? error.message : 'Firmware campaign could not be created.')
    } finally {
      setBusy(false)
    }
  }

  const revoke = async () => {
    if (!revokeRelease || revokeReason.trim().length < 10 || !revokePassword) return
    setRevokeBusy(true)
    try {
      await api(`/api/v1/firmware/releases/${revokeRelease.release_id}/revoke`, {
        method: 'POST',
        body: JSON.stringify({ reason: revokeReason.trim(), password: revokePassword }),
      })
      toast.notice(`Release ${revokeRelease.version} is revoked and active campaigns are paused.`)
      setRevokeRelease(null)
      setRevokeReason('')
      setRevokePassword('')
      await load()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Firmware release could not be revoked.')
    } finally {
      setRevokeBusy(false)
    }
  }

  const activeCampaigns = campaigns.filter((campaign) => ['ACTIVE', 'PAUSED'].includes(campaign.status))
  const newestRelease = releases.find((release) => release.state !== 'REVOKED') || null

  return (
    <>
      <PageHeader
        eyebrow="SIGNED OTA CONTROL PLANE"
        title="Firmware operations"
        description="Operate signed Zone Lite releases with exact scope, visible rollout evidence, and audited controls."
        action={<div className="page-actions"><button className="button secondary" onClick={() => void load()}><Icon name="refresh" /> Refresh</button><button className="button primary" onClick={() => setCreatorOpen(true)}><Icon name="plus" /> New campaign</button></div>}
      />
      <div className="firmware-mode-grid">
        <article className={`firmware-mode pattern-${enabled ? 'confirmed' : 'blocked'}`}>
          <Icon name="shield" />
          <span><strong>National OTA</strong><small>{enabled ? 'Enabled for AVAILABLE releases' : 'Disabled'}</small></span>
        </article>
        <article className={`firmware-mode pattern-${hilEnabled ? 'confirmed' : 'blocked'}`}>
          <Icon name="terminal" />
          <span><strong>Quarantined HIL OTA</strong><small>{hilEnabled ? 'Enabled for exact target MAC only' : 'Disabled'}</small></span>
        </article>
      </div>

      <nav className="section-tabs" aria-label="Firmware sections">
        {(['overview', 'prepare', 'releases', 'campaigns'] as const).map((value) => <button key={value} className={section === value ? 'active' : ''} aria-current={section === value ? 'page' : undefined} onClick={() => onSection(value)}>{value === 'overview' ? 'Overview' : value === 'prepare' ? 'Prepare device' : value === 'releases' ? `Signed releases (${releases.length})` : `Campaigns (${campaigns.length})`}</button>)}
      </nav>

      {section === 'overview' && <section className="firmware-overview-grid">
        <article className="overview-hero"><p className="eyebrow">RELEASE POSTURE</p><h2>{newestRelease ? `Zone Lite ${newestRelease.version}` : 'No deployable release'}</h2><p>{newestRelease ? `Published ${dateTime(newestRelease.published_at)} · ${newestRelease.partition_layout}` : 'Publish a signed release before creating a production campaign.'}</p><div className="page-actions"><button className="button primary" onClick={() => onSection('prepare')}><Icon name="plus" /> Prepare ESP32</button><button className="text-button" onClick={() => onSection('releases')}>Review release inventory <Icon name="chevron" /></button></div></article>
        <article><span className="overview-icon"><Icon name="pulse" /></span><div><p>Active campaigns</p><strong>{activeCampaigns.length}</strong><small>{activeCampaigns.filter((campaign) => campaign.status === 'PAUSED').length} paused</small></div></article>
        <article><span className="overview-icon"><Icon name="server" /></span><div><p>OTA-ready fleet</p><strong>{devices.filter((device) => device.ota_capable).length}/{devices.length}</strong><small>{devices.filter((device) => !device.ota_capable).length} legacy or blocked</small></div></article>
      </section>}

      {creatorOpen && <Dialog titleId="firmware-campaign-title" title="Create firmware campaign" description="Choose signed bytes, verify the exact scope, then complete administrator step-up." onClose={() => { setCreatorOpen(false); setPassword(''); setConfirmation(''); setReason('') }} className="firmware-campaign-dialog"><div className="firmware-start-panel">
        <div className="panel-header">
          <div><p className="eyebrow">1 RELEASE · 2 SCOPE · 3 CONFIRM</p><h2>Constrained rollout</h2><p>Server-side eligibility remains authoritative when the campaign is created.</p></div>
          <StatusBadge state={isHilRelease ? 'HIL_ONLY' : selectedRelease?.state || 'NO RELEASE SELECTED'} />
        </div>
        <form className="firmware-start-form" onSubmit={(event) => void start(event)}>
          <label>
            Signed release
            <select value={releaseId} onChange={(event) => selectRelease(event.target.value)}>
              <option value="">Select a release</option>
              {releases.map((release) => (
                <option key={release.release_id} value={release.release_id} disabled={release.state === 'REVOKED'}>
                  {release.version} · {release.state}
                </option>
              ))}
            </select>
          </label>
          <label>
            Zone
            <select
              value={zoneId}
              disabled={isHilRelease}
              onChange={(event) => { setZoneId(event.target.value); setScopePreview(null) }}
            >
              <option value="">Select a zone</option>
              {zones.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
            </select>
          </label>
          <label className="firmware-reason">
            Audited reason
            <input
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="At least 10 characters"
            />
          </label>
          <label>
            Type release version to confirm
            <input
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
              placeholder={selectedRelease?.version || 'Select a release first'}
            />
          </label>
          <label>
            Administrator password
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          {isHilRelease && (
            <div className={`firmware-quarantine pattern-${hilTargetDevice ? 'confirmed' : 'blocked'}`}>
              <Icon name={hilTargetDevice ? 'shield' : 'alert'} />
              <div>
                <strong>HIL quarantine is immutable for this release</strong>
                <p>
                  Only <code>{selectedRelease?.hil_target_mac || 'missing target MAC'}</code>
                  {hilTargetDevice ? ` in ${hilTargetDevice.zone_id}` : ' matches no registered device'} is eligible.
                </p>
              </div>
            </div>
          )}
          {selectedRelease && zoneId && !scopePreview && <div className="firmware-scope-action"><button className="button secondary" type="button" disabled={previewBusy} onClick={() => void previewScope()}><Icon name="search" /> {previewBusy ? 'Calculating server scope…' : 'Preview exact server scope'}</button><small>Required before confirmation. The signed scope expires after five minutes and fails if eligibility changes.</small></div>}
          {scopePreview && <section className="firmware-scope-preview" aria-live="polite"><div className="panel-header"><div><p className="eyebrow">SERVER-AUTHORITATIVE SCOPE</p><h3>{scopePreview.counts.eligible} eligible of {scopePreview.counts.candidates} candidates</h3><p>{scopePreview.counts.excluded} excluded · {scopePreview.counts.offline} currently offline · expires {dateTime(scopePreview.expires_at)}</p></div><StatusBadge state={scopePreview.counts.eligible ? 'SCOPE VERIFIED' : 'NO ELIGIBLE DEVICES'} /></div>{scopePreview.excluded.length > 0 && <details><summary>Review {scopePreview.excluded.length} excluded device{scopePreview.excluded.length === 1 ? '' : 's'}</summary><ul>{scopePreview.excluded.map((device) => <li key={device.connector_id}><strong>{device.display_name}</strong><span>{device.reason.replaceAll('_', ' ')}</span></li>)}</ul></details>}<button className="text-button" type="button" onClick={() => void previewScope()}><Icon name="refresh" /> Refresh scope</button></section>}
          <button className="button primary firmware-start-button" type="submit" disabled={!startAllowed}>
            <Icon name="power" /> {busy ? 'Starting audited campaign…' : 'Start firmware campaign'}
          </button>
        </form>
      </div></Dialog>}

      {section === 'releases' && <section className="panel firmware-section">
        <div className="panel-header"><div><h2>Signed release inventory</h2><p>Cryptographic identity and publication state from the server-side release store.</p></div></div>
        <div className="firmware-release-list" aria-busy={loading}>
          {releases.map((release) => (
            <article key={release.release_id} className="firmware-release">
              <div className="firmware-release-title">
                <div><p className="eyebrow">ZONE LITE {release.version}</p><h3>{release.release_id}</h3></div>
                <StatusBadge state={release.state} />
              </div>
              <dl>
                <div><dt>Git commit</dt><dd><code>{release.git_sha}</code></dd></div>
                <div><dt>Image SHA-256</dt><dd><code>{release.image_sha256}</code></dd></div>
                <div><dt>Application SHA-256</dt><dd><code>{release.application_sha256 || '—'}</code></dd></div>
                <div><dt>Signing key</dt><dd><code>{release.signing_key_id}</code></dd></div>
                <div><dt>Partition layout</dt><dd>{release.partition_layout}</dd></div>
                <div><dt>Image size</dt><dd>{new Intl.NumberFormat('en-PK').format(release.image_size)} bytes</dd></div>
                <div><dt>Published</dt><dd>{dateTime(release.published_at)}</dd></div>
                <div><dt>HIL target</dt><dd><code>{release.hil_target_mac || 'Not quarantined'}</code></dd></div>
              </dl>
              {release.state !== 'REVOKED' && <div className="firmware-release-actions"><button className="button destructive" onClick={() => setRevokeRelease(release)}><Icon name="alert" /> Revoke release</button></div>}
            </article>
          ))}
          {!loading && !releases.length && <div className="empty-state"><Icon name="terminal" /><h3>No signed firmware releases are published.</h3></div>}
        </div>
      </section>}

      {section === 'campaigns' && <section className="panel firmware-section">
        <div className="panel-header"><div><h2>Campaign history and controls</h2><p>Deployment counts are durable server state; pause, resume, and cancel require step-up authentication.</p></div></div>
        <div className="firmware-campaign-list" aria-busy={loading}>
          {campaigns.map((campaign) => (
            <article key={campaign.campaign_id} className="firmware-campaign">
              <div className="firmware-campaign-summary">
                <div><p className="eyebrow">{campaign.zone_id}</p><h3>{campaign.version || 'Unknown release'}</h3><code>{campaign.campaign_id}</code></div>
                <StatusBadge state={campaign.status} />
                <div><strong>{campaign.eligible}</strong><small>Eligible · {campaign.legacy_skipped} skipped</small></div>
                <div><strong>{Object.entries(campaign.counts).map(([state, count]) => `${state.replaceAll('_', ' ')} ${count}`).join(' · ') || 'No offers yet'}</strong><small>Created {dateTime(campaign.created_at)}</small></div>
              </div>
              {campaign.pause_reason && <p className="firmware-pause-reason"><Icon name="alert" /> {campaign.pause_reason}</p>}
              {campaign.deployments?.map((deployment) => (
                <details className="firmware-deployment-evidence" key={deployment.deployment_id}>
                  <summary>
                    {deployment.connector_id || 'Unknown connector'} · {deployment.status.replaceAll('_', ' ')}
                    {deployment.error_code ? ` · ${deployment.error_code}` : ''}
                  </summary>
                  <dl>
                    <div><dt>Deployment</dt><dd><code>{deployment.deployment_id}</code></dd></div>
                    <div><dt>Version</dt><dd>{deployment.previous_version || 'Unknown'} → {deployment.target_version}</dd></div>
                    <div><dt>Bytes</dt><dd>{new Intl.NumberFormat('en-PK').format(deployment.bytes_written)}</dd></div>
                    <div><dt>Attempts</dt><dd>{deployment.attempt_count}</dd></div>
                  </dl>
                  {deployment.events.length > 0 && (
                    <ol className="firmware-deployment-events">
                      {deployment.events.map((event, index) => (
                        <li key={`${event.created_at}-${index}`}>
                          <time>{dateTime(event.created_at)}</time>
                          <strong>{event.state.replaceAll('_', ' ')}</strong>
                          {event.details.error_code ? <code>{String(event.details.error_code)}</code> : null}
                        </li>
                      ))}
                    </ol>
                  )}
                </details>
              ))}
              <FirmwareCampaignControls campaign={campaign} onChanged={load} toast={toast} />
            </article>
          ))}
          {!loading && !campaigns.length && <div className="empty-state"><Icon name="shield" /><h3>No firmware campaigns have been started.</h3></div>}
        </div>
      </section>}

      {revokeRelease && <Dialog titleId="revoke-firmware-title" title={`Revoke Zone Lite ${revokeRelease.version}`} description="Revocation blocks new offers and pauses every active campaign for these signed bytes." onClose={() => { setRevokeRelease(null); setRevokeReason(''); setRevokePassword('') }}><div className="dialog-body"><div className="destructive-copy pattern-blocked"><Icon name="alert" /><div><h3>This release will no longer be deployable.</h3><p>Devices already applying bytes continue according to durable deployment safety state; operators must review them explicitly.</p></div></div><label>Audited revocation reason<textarea value={revokeReason} onChange={(event) => setRevokeReason(event.target.value)} maxLength={200} rows={3} /></label><label>Administrator password<input type="password" autoComplete="current-password" value={revokePassword} onChange={(event) => setRevokePassword(event.target.value)} /></label><div className="dialog-actions"><button className="button secondary" onClick={() => setRevokeRelease(null)}>Keep release available</button><button className="button destructive" disabled={revokeBusy || revokeReason.trim().length < 10 || !revokePassword} onClick={() => void revoke()}>{revokeBusy ? 'Revoking…' : 'Revoke signed release'}</button></div></div></Dialog>}
    </>
  )
}
