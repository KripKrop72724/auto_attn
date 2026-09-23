import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, queryString } from '../api'
import { Metric, StatusBadge, idempotency, relativeTime, useToast } from '../App'
import { Icon } from '../Icon'
import type {
  AttendanceRecoveryAction,
  AttendanceRecoveryJob,
  AttendanceRecoveryPreview,
  AttendanceRecoverySummary,
  SourceCorrectionCandidate,
  SourceExceptionList,
} from '../types'

type Toast = ReturnType<typeof useToast>

const actions: Array<{ value: AttendanceRecoveryAction; label: string; description: string }> = [
  { value: 'RETRY_DELIVERY', label: 'Retry safe delivery', description: 'Queue eligible events through the existing idempotent ORDS outbox.' },
  { value: 'REBUILD_OUTBOX', label: 'Rebuild missing outbox', description: 'Create only missing durable delivery rows.' },
  { value: 'RECOVER_STALE_IN_FLIGHT', label: 'Recover stale in-flight', description: 'Requeue deliveries whose worker lease has expired.' },
]

function formatDate(value: string | null) {
  if (!value) return '—'
  return new Date(value).toLocaleString()
}

export function AttendanceRecovery({ revision, toast }: { revision: number; toast: Toast }) {
  const [summary, setSummary] = useState<AttendanceRecoverySummary | null>(null)
  const [summaryError, setSummaryError] = useState('')
  const [action, setAction] = useState<AttendanceRecoveryAction>('RETRY_DELIVERY')
  const [zone, setZone] = useState('')
  const [terminal, setTerminal] = useState('')
  const [family, setFamily] = useState('')
  const [status, setStatus] = useState('')
  const [sourceEpoch, setSourceEpoch] = useState('')
  const [fromAt, setFromAt] = useState('')
  const [toAt, setToAt] = useState('')
  const [staleOnly, setStaleOnly] = useState(false)
  const [errorCategory, setErrorCategory] = useState('')
  const [provenance, setProvenance] = useState('')
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [preview, setPreview] = useState<AttendanceRecoveryPreview | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [busy, setBusy] = useState(false)
  const [job, setJob] = useState<AttendanceRecoveryJob | null>(null)
  const [sourceExceptions, setSourceExceptions] = useState<SourceExceptionList | null>(null)
  const [hikvisionCandidates, setHikvisionCandidates] = useState<SourceCorrectionCandidate[]>([])
  const [sourceError, setSourceError] = useState('')
  const [selectedExceptions, setSelectedExceptions] = useState<Set<number>>(new Set())
  const [selectedHikvision, setSelectedHikvision] = useState<Set<number>>(new Set())
  const [correctionTimes, setCorrectionTimes] = useState<Record<number, string>>({})
  const [hikvisionCorrectionTimes, setHikvisionCorrectionTimes] = useState<Record<number, string>>({})
  const [correctionPreview, setCorrectionPreview] = useState<any>(null)
  const [controlBusy, setControlBusy] = useState(false)
  const [controlConfirmation, setControlConfirmation] = useState('')

  const loadSummary = useCallback(async () => {
    try {
      setSummaryError('')
      setSummary(await api<AttendanceRecoverySummary>('/api/v2/attendance-recovery/summary'))
    } catch (error) {
      setSummaryError(error instanceof Error ? error.message : 'Recovery summary could not be loaded.')
    }
  }, [])

  const loadSourceExceptions = useCallback(async () => {
    try {
      setSourceError('')
      const [zkt, hik] = await Promise.all([
        api<SourceExceptionList>('/api/v1/source-exceptions' + queryString({ review_state: 'OPEN', limit: 50 })),
        api<{ rows: SourceCorrectionCandidate[] }>('/api/v2/source-corrections/candidates?limit=50'),
      ])
      setSourceExceptions(zkt)
      setHikvisionCandidates(hik.rows)
    } catch (error) {
      setSourceError(error instanceof Error ? error.message : 'Source corrections could not be loaded.')
    }
  }, [])

  useEffect(() => {
    void loadSummary()
    void loadSourceExceptions()
  }, [loadSourceExceptions, loadSummary, revision])

  useEffect(() => {
    if (!job || ['COMPLETED', 'COMPLETED_WITH_ATTENTION', 'CANCELLED'].includes(job.status)) return
    const timer = window.setInterval(() => {
      void api<AttendanceRecoveryJob>(`/api/v2/attendance-recovery/jobs/${job.job_id}`).then(setJob).catch(() => undefined)
    }, 2500)
    return () => window.clearInterval(timer)
  }, [job])

  const filters = useMemo(() => ({
    zone_id: zone || undefined,
    terminal_serial: terminal || undefined,
    firmware_family: family || undefined,
    lane: 'SAFE_DELIVERY',
    statuses: status ? [status] : [],
    source_epoch: sourceEpoch || undefined,
    from_at: fromAt ? new Date(fromAt).toISOString() : undefined,
    to_at: toAt ? new Date(toAt).toISOString() : undefined,
    stale_only: staleOnly,
    ords_error_category: errorCategory || undefined,
    provenance: provenance || undefined,
  }), [errorCategory, family, fromAt, provenance, sourceEpoch, staleOnly, status, terminal, toAt, zone])

  const runPreview = async () => {
    setBusy(true)
    setPreviewError('')
    try {
      const result = await api<AttendanceRecoveryPreview>('/api/v2/attendance-recovery/preview', {
        method: 'POST',
        body: JSON.stringify({ action, filters, reason: reason.trim() || 'Preview safe attendance delivery recovery.' }),
      })
      setPreview(result)
      setConfirmation(result.confirmation)
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : 'The recovery preview could not be generated.')
    } finally {
      setBusy(false)
    }
  }

  const createJob = async () => {
    if (!preview) return
    setBusy(true)
    setPreviewError('')
    try {
      const result = await api<AttendanceRecoveryJob>('/api/v2/attendance-recovery/jobs', {
        method: 'POST',
        body: JSON.stringify({ action, filters, reason, password, typed_confirmation: confirmation, candidate_digest: preview.candidate_digest, preview_expires_at: preview.preview_expires_at, preview_signature: preview.preview_signature, idempotency_key: idempotency('attendance-recovery') }),
      })
      setJob(result)
      toast.notice('Recovery batch queued. No source rows were changed.')
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : 'The recovery batch could not be queued.')
    } finally {
      setBusy(false)
    }
  }

  const correctionInputs = [...selectedExceptions]
    .filter((id) => correctionTimes[id])
    .map((id) => ({
      source_kind: 'ZKT_MANIFEST',
      source_ref: String(id),
      corrected_device_time: new Date(correctionTimes[id]).toISOString(),
    }))
    .concat(
      [...selectedHikvision]
        .filter((id) => hikvisionCorrectionTimes[id])
        .map((id) => ({
          source_kind: 'HIKVISION_EVIDENCE',
          source_ref: String(id),
          corrected_device_time: new Date(hikvisionCorrectionTimes[id]).toISOString(),
        })),
    )

  const previewCorrections = async () => {
    if (!correctionInputs.length) return
    setBusy(true)
    try {
      const result = await api<any>('/api/v2/source-corrections/preview', {
        method: 'POST',
        body: JSON.stringify({ corrections: correctionInputs, reason: reason.trim() || 'Preview timestamp correction.' }),
      })
      setCorrectionPreview(result)
      setConfirmation(result.confirmation)
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : 'The correction preview could not be generated.')
    } finally {
      setBusy(false)
    }
  }

  const createCorrections = async () => {
    if (!correctionPreview) return
    setBusy(true)
    try {
      const result = await api<AttendanceRecoveryJob>('/api/v2/source-corrections/jobs', {
        method: 'POST',
        body: JSON.stringify({ corrections: correctionInputs, reason, password, typed_confirmation: confirmation, candidate_digest: correctionPreview.candidate_digest, preview_expires_at: correctionPreview.preview_expires_at, preview_signature: correctionPreview.preview_signature, idempotency_key: idempotency('attendance-source-correction') }),
      })
      setJob(result)
      toast.notice('Timestamp correction batch queued. Original source evidence remains immutable.')
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : 'The correction batch could not be queued.')
    } finally {
      setBusy(false)
    }
  }

  const toggleException = (id: number) => setSelectedExceptions((current) => {
    const next = new Set(current)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })
  const toggleHikvision = (id: number) => setSelectedHikvision((current) => {
    const next = new Set(current)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  const controlJob = async (control: 'pause' | 'resume' | 'cancel' | 'retry') => {
    if (!job || !password || reason.trim().length < 10) return
    const expectedConfirmation = `${control.toUpperCase()} ${job.job_id}`
    if (controlConfirmation.trim() !== expectedConfirmation) {
      setPreviewError(`Type ${expectedConfirmation} to confirm this job control action.`)
      return
    }
    setControlBusy(true)
    try {
      const updated = await api<AttendanceRecoveryJob>(`/api/v2/attendance-recovery/jobs/${job.job_id}/control`, {
        method: 'POST',
        body: JSON.stringify({ action: control, reason, password, candidate_digest: job.candidate_digest, typed_confirmation: controlConfirmation.trim(), idempotency_key: idempotency(`attendance-recovery-${control}`) }),
      })
      setJob(updated)
      setControlConfirmation('')
      toast.notice(`Recovery job ${control} request accepted.`)
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : 'The recovery job control request failed.')
    } finally {
      setControlBusy(false)
    }
  }

  const delivery = summary?.delivery
  return (
    <div className="reconciliation-recovery">
      <section className="metric-grid reconciliation-metrics" aria-label="Recovery health">
        <Metric label="Safe delivery" value={(delivery?.safe_retryable ?? 0).toLocaleString()} detail="Eligible for preview and bulk retry" icon="refresh" tone={delivery?.safe_retryable ? 'warning' : 'positive'} />
        <Metric label="Missing outbox" value={(delivery?.missing_outbox ?? 0).toLocaleString()} detail="Durable rows to rebuild" icon="server" tone={delivery?.missing_outbox ? 'warning' : 'positive'} />
        <Metric label="Active in-flight" value={(delivery?.active_in_flight ?? 0).toLocaleString()} detail="Owned by a live worker lease" icon="pulse" tone="neutral" />
        <Metric label="Identity held" value={(delivery?.identity_held ?? 0).toLocaleString()} detail="Read-only evidence lane" icon="shield" tone={delivery?.identity_held ? 'warning' : 'positive'} />
        <Metric label="Source review" value={(delivery?.permanent_review ?? 0).toLocaleString()} detail={`${summary?.source.zkt_invalid_time ?? 0} invalid ZKT timestamps`} icon="alert" tone={delivery?.permanent_review ? 'critical' : 'positive'} />
      </section>
      {(summaryError || previewError) && <div className="inline-retry pattern-blocked" role="alert"><Icon name="alert" /><span>{summaryError || previewError}</span><button className="button secondary" onClick={() => void loadSummary()}>Retry</button></div>}
      <section className="panel recovery-action-panel">
        <div className="panel-header"><div><p className="eyebrow">SAFE DELIVERY LANE</p><h2>Recover preserved attendance in bulk</h2><p>Identity-held and unsafe rows are automatically excluded. Preview freezes the exact rows before approval.</p></div><StatusBadge state={summary?.execution_enabled ? 'READY' : 'PREVIEW ONLY'} /></div>
        <div className="reconciliation-toolbar">
          <label><span>Action</span><select value={action} onChange={(event) => { setAction(event.target.value as AttendanceRecoveryAction); setPreview(null) }}>{actions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
          <label><span>Vendor</span><select value={family} onChange={(event) => setFamily(event.target.value)}><option value="">ZKT and HIK</option><option value="zkt">ZKT</option><option value="hikvision">Hikvision</option></select></label>
          <label><span>Zone</span><input value={zone} onChange={(event) => setZone(event.target.value)} placeholder="ZONE-LAHORE-01" /></label>
          <label><span>Terminal serial</span><input value={terminal} onChange={(event) => setTerminal(event.target.value)} placeholder="Optional exact serial" /></label>
          <label><span>Status</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All active statuses</option><option value="PENDING">Pending</option><option value="FAILED_RETRYABLE">Retryable failure</option><option value="BLOCKED_IDENTITY">Identity-held</option><option value="IN_FLIGHT">In-flight</option></select></label>
          <label><span>Source epoch</span><input value={sourceEpoch} onChange={(event) => setSourceEpoch(event.target.value)} placeholder="Optional epoch" /></label>
          <label><span>ORDS error</span><input value={errorCategory} onChange={(event) => setErrorCategory(event.target.value)} placeholder="Optional category" /></label>
          <label><span>From</span><input type="datetime-local" value={fromAt} onChange={(event) => setFromAt(event.target.value)} /></label>
          <label><span>To</span><input type="datetime-local" value={toAt} onChange={(event) => setToAt(event.target.value)} /></label>
          <label><span>Provenance</span><select value={provenance} onChange={(event) => setProvenance(event.target.value)}><option value="">All</option><option value="VERIFIED">Verified</option><option value="MISSING">Missing</option></select></label>
          <label className="recovery-inline-check"><input type="checkbox" checked={staleOnly} onChange={(event) => setStaleOnly(event.target.checked)} /><span>Only expired in-flight</span></label>
        </div>
        <p className="recovery-action-description">{actions.find((item) => item.value === action)?.description}</p>
        <div className="recovery-confirm-fields"><label><span>Audit reason</span><textarea value={reason} onChange={(event) => setReason(event.target.value)} minLength={10} placeholder="Why is this recovery batch being run?" /></label><label><span>Administrator password</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label></div>
        <div className="dialog-actions"><button className="button primary" disabled={busy || reason.trim().length < 10} onClick={() => void runPreview()}><Icon name="search" /> {busy ? 'Preparing…' : 'Preview filtered rows'}</button></div>
        {preview && <div className="recovery-preview" aria-live="polite"><div className="recovery-preview-counts"><strong>{preview.counts.eligible.toLocaleString()} eligible</strong><span>{preview.counts.excluded.toLocaleString()} excluded</span><span>{preview.counts.identity_held.toLocaleString()} identity-held</span><span>{preview.counts.permanent_review.toLocaleString()} review-only</span></div><p>Candidate digest <code>{preview.candidate_digest.slice(0, 16)}…</code> · expires {relativeTime(preview.preview_expires_at)}</p><label><span>Type confirmation: <strong>{preview.confirmation}</strong></span><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label><button className="button primary" disabled={busy || !password || confirmation !== preview.confirmation || !summary?.execution_enabled} onClick={() => void createJob()}>Approve and queue batch</button><div className="recovery-candidate-table">{preview.rows.slice(0, 25).map((row) => <div className="recovery-candidate-row" key={row.source_ref}><StatusBadge state={row.event_status || 'PENDING'} /><span>{row.display_name} · {row.zone_id}</span><small>{row.terminal_serial || 'Terminal provenance pending'} · {formatDate(row.device_event_time)}</small></div>)}{preview.counts.eligible > 25 && <small>Showing first 25 rows; the approved digest covers all matching eligible rows.</small>}</div></div>}
      </section>
      <section className="panel recovery-identity-panel"><div className="panel-header"><div><p className="eyebrow">MANUAL APPROVAL REQUIRED</p><h2>Identity-held events stay protected</h2><p>Use Force release attendance to sync current terminal users and review eligible punches. An administrator must approve their release with a password and reason. Syncing users alone does not release held attendance.</p></div><StatusBadge state="READ ONLY" /></div><div className="info-copy pattern-waiting"><Icon name="shield" /><span>{(delivery?.identity_held ?? 0).toLocaleString()} identity-held event(s) remain saved for review.</span></div><div className="dialog-actions"><a className="button secondary" href="/attendance?view=needs-review"><Icon name="users" /> Open force release</a></div></section>
      <section className="panel recovery-correction-panel"><div className="panel-header"><div><p className="eyebrow">SOURCE CORRECTION LANE</p><h2>Correct eligible terminal timestamps</h2><p>Only parseable identity plus timestamp-only errors may be corrected. Original source bytes remain immutable.</p></div><StatusBadge state={`${(sourceExceptions?.totals.open ?? 0) + hikvisionCandidates.length} OPEN`} /></div>{sourceError && <div className="inline-retry pattern-blocked" role="alert"><Icon name="alert" /><span>{sourceError}</span></div>}<p className="recovery-correction-heading">ZKT source manifests</p>{sourceExceptions?.rows.map((row) => <div className="recovery-correction-row" key={`zkt-${row.id}`}><label className="recovery-checkbox"><input type="checkbox" checked={selectedExceptions.has(row.id)} onChange={() => toggleException(row.id)} /><span>{row.display_name || row.terminal_serial} · ordinal {row.ordinal}</span></label><small>{row.error_code || 'INVALID_TIME'} · UID {row.observed_uid || 'unparsed'} · source {row.raw_record_digest.slice(0, 12)}…</small><input type="datetime-local" disabled={!selectedExceptions.has(row.id)} value={correctionTimes[row.id] || ''} onChange={(event) => setCorrectionTimes((current) => ({ ...current, [row.id]: event.target.value }))} /></div>)}<p className="recovery-correction-heading">Hikvision evidence</p>{hikvisionCandidates.map((row) => <div className="recovery-correction-row" key={`hik-${row.id}`}><label className="recovery-checkbox"><input type="checkbox" disabled={!row.eligible} checked={selectedHikvision.has(row.id)} onChange={() => toggleHikvision(row.id)} /><span>{row.display_name} · {row.terminal_serial} · epoch {row.source_epoch}</span></label><small>{row.eligible ? 'Identity verified; timestamp-only correction is eligible' : `${row.disposition} · inspect evidence; no correction permitted`} · digest {row.observation_sha256.slice(0, 12)}…</small><input type="datetime-local" disabled={!row.eligible || !selectedHikvision.has(row.id)} value={hikvisionCorrectionTimes[row.id] || ''} onChange={(event) => setHikvisionCorrectionTimes((current) => ({ ...current, [row.id]: event.target.value }))} /></div>)}{sourceExceptions && !sourceExceptions.rows.length && !hikvisionCandidates.length && <div className="empty-state compact"><Icon name="check" /><p>No open source evidence rows are loaded for correction or review.</p></div>}<div className="dialog-actions"><button className="button secondary" disabled={busy || !correctionInputs.length} onClick={() => void previewCorrections()}><Icon name="search" /> Preview timestamp corrections</button></div>{correctionPreview && <div className="recovery-preview"><div className="recovery-preview-counts"><strong>{correctionPreview.counts.eligible} eligible</strong><span>{correctionPreview.counts.rejected} rejected</span></div><p>Type confirmation: <strong>{correctionPreview.confirmation}</strong></p><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /><button className="button primary" disabled={busy || !password || confirmation !== correctionPreview.confirmation || !summary?.execution_enabled || !summary?.correction_enabled} onClick={() => void createCorrections()}>Approve timestamp corrections</button>{!summary?.correction_enabled && <small className="recovery-disabled-reason">Timestamp correction remains disabled until the canary proves its lineage.</small>}{correctionPreview.rejected?.map((item: { source_ref: string; reason: string }) => <p className="recovery-rejected" key={item.source_ref}>{item.source_ref}: {item.reason}</p>)}</div>}</section>
      {job && <section className="panel recovery-job-panel"><div className="panel-header"><div><p className="eyebrow">DURABLE RECOVERY JOB</p><h2>{job.action.replaceAll('_', ' ')}</h2><p>{job.job_id} · updated {relativeTime(job.updated_at)}</p></div><StatusBadge state={job.status} live={job.status === 'RUNNING'} /></div><div className="recovery-progress"><strong>{job.progress.succeeded.toLocaleString()} succeeded</strong><span>{job.progress.failed.toLocaleString()} failed</span><span>{job.progress.skipped.toLocaleString()} skipped</span><span>{job.progress.requested.toLocaleString()} requested</span></div><div className="recovery-control-confirm"><label><span>Type the exact phrase for the control action you choose</span><input value={controlConfirmation} onChange={(event) => setControlConfirmation(event.target.value)} placeholder={`${job.status === 'PAUSED' ? 'RESUME' : 'PAUSE'} ${job.job_id}`} autoComplete="off" /></label><small>Use the action word followed by the full job ID.</small></div><div className="dialog-actions recovery-job-controls">{['QUEUED', 'RUNNING', 'PAUSE_REQUESTED'].includes(job.status) && <button className="button secondary" disabled={controlBusy || controlConfirmation.trim() !== `PAUSE ${job.job_id}`} onClick={() => void controlJob('pause')}>Pause</button>}{job.status === 'PAUSED' && <button className="button secondary" disabled={controlBusy || controlConfirmation.trim() !== `RESUME ${job.job_id}`} onClick={() => void controlJob('resume')}>Resume</button>}{job.status === 'COMPLETED_WITH_ATTENTION' && <button className="button secondary" disabled={controlBusy || controlConfirmation.trim() !== `RETRY ${job.job_id}`} onClick={() => void controlJob('retry')}>Retry failed safe items</button>}{!['COMPLETED', 'CANCELLED'].includes(job.status) && <button className="button danger" disabled={controlBusy || controlConfirmation.trim() !== `CANCEL ${job.job_id}`} onClick={() => void controlJob('cancel')}>Cancel pending items</button>}</div>{job.items && job.items.filter((item) => item.status !== 'SUCCEEDED').slice(0, 50).map((item) => <div className="recovery-candidate-row" key={item.item_id}><StatusBadge state={item.status} /><span>{item.source_ref} · {item.outcome || 'pending'}</span><small>{item.error_code || 'Awaiting worker'} {item.error_message || ''}</small></div>)}</section>}
    </div>
  )
}
