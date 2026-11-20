// "Not in HR system yet" (08-advanced 1.7, ADV-8; today the "Recovery" tab).
// Plain rows say what is waiting to reach the HR system and why. The bulk
// resend form is gone: its server endpoints were retired in #188 and answer
// 410, so it could only ever fail. The identity panel is now one link row.
// "Fix impossible times" keeps its preview → approve flow and its safeguard
// (note, your password and the typed phrase) exactly; only its words, labels
// and the Pakistan-time dates change.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, queryString } from '../api'
import { Button, Callout, ErrorState, Skeleton, StatusChip, TechnicalDetails } from '../ui'
import { idempotency } from '../lib/idempotency'
import { ago, dayTime, pktInputToUtc } from '../lib/time'
import { useThrottledRunner } from '../lib/throttle'
import { formatMetricsTime } from '../lib/metricsTime'
import { formatCount } from '../words/glossary'
import { FIX_TIMES, HELD_TAB, notLinedUpText, recoveryJob, setAsideText } from '../words/recover'
import type { Toast } from '../ui/Toast'
import type {
  AttendanceRecoveryJob,
  AttendanceRecoverySummary,
  SourceCorrectionCandidate,
  SourceExceptionList,
} from '../types'
import { chipTone } from './recover/tones'

type RecoveryDelivery = AttendanceRecoverySummary['delivery']

/**
 * Only a real gap colours the "Not lined up for sending yet" row: held and
 * set-aside rows never get a sending slot until they are released, so they are
 * named beside the number, never inside it.
 */
export function notLinedUpTone(delivery?: RecoveryDelivery): 'warning' | 'neutral' {
  return delivery?.missing_outbox ? 'warning' : 'neutral'
}

// Automatic (realtime) refreshes of the summary and the time-fix lists start at
// most this often and never overlap. The summary counts the whole attendance
// and HR system backlog, which is heavy during a large release or recovery.
export const RECOVERY_SUMMARY_REFRESH_MS = 15_000

type CorrectionPreview = {
  counts: { eligible: number; rejected: number }
  confirmation: string
  candidate_digest: string
  preview_expires_at: string
  preview_signature: string
  rejected?: Array<{ source_ref: string; reason: string }>
}

function SummaryRow({ label, value, sentence, tone = 'neutral', link }: { label: string; value: number | null; sentence?: string; tone?: 'warning' | 'neutral'; link?: { label: string; href?: string; onClick?: () => void } }) {
  return (
    <li className="recover-held-row" data-tone={tone}>
      <span className="recover-held-label">{label}</span>
      <strong className="recover-held-value">{value === null ? <Skeleton variant="line" width="56px" /> : formatCount(value)}</strong>
      <span className="recover-held-text">
        {sentence}
        {link && (link.href
          ? <a className="text-button" href={link.href}>{link.label} ›</a>
          : <button type="button" className="text-button" onClick={link.onClick}>{link.label} ›</button>)}
      </span>
    </li>
  )
}

export function AttendanceRecovery({
  revision,
  toast,
  refreshToken = 0,
  onSummary,
  onShowUnreadable,
}: {
  revision: number
  toast: Toast
  /** Changes when the person presses the page Refresh. */
  refreshToken?: number
  onSummary?: (summary: AttendanceRecoverySummary) => void
  onShowUnreadable?: () => void
}) {
  const [summary, setSummary] = useState<AttendanceRecoverySummary | null>(null)
  const [summaryError, setSummaryError] = useState<unknown>(null)
  const [sourceExceptions, setSourceExceptions] = useState<SourceExceptionList | null>(null)
  const [hikvisionCandidates, setHikvisionCandidates] = useState<SourceCorrectionCandidate[]>([])
  const [sourceError, setSourceError] = useState<unknown>(null)
  const [selectedExceptions, setSelectedExceptions] = useState<Set<number>>(new Set())
  const [selectedHikvision, setSelectedHikvision] = useState<Set<number>>(new Set())
  const [correctionTimes, setCorrectionTimes] = useState<Record<number, string>>({})
  const [hikvisionCorrectionTimes, setHikvisionCorrectionTimes] = useState<Record<number, string>>({})
  const [correctionPreview, setCorrectionPreview] = useState<CorrectionPreview | null>(null)
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [actionError, setActionError] = useState('')
  const [busy, setBusy] = useState(false)
  const [job, setJob] = useState<AttendanceRecoveryJob | null>(null)
  const [controlBusy, setControlBusy] = useState(false)
  const [controlConfirmation, setControlConfirmation] = useState('')

  const onSummaryRef = useRef(onSummary)
  onSummaryRef.current = onSummary
  const loadSummary = useCallback(async () => {
    try {
      setSummaryError(null)
      const result = await api<AttendanceRecoverySummary>('/api/v2/attendance-recovery/summary')
      setSummary(result)
      onSummaryRef.current?.(result)
    } catch (error) {
      setSummaryError(error)
    }
  }, [])

  const loadSourceExceptions = useCallback(async () => {
    try {
      setSourceError(null)
      const [zkt, hik] = await Promise.all([
        api<SourceExceptionList>('/api/v1/source-exceptions' + queryString({ review_state: 'OPEN', limit: 50 })),
        api<{ rows: SourceCorrectionCandidate[] }>('/api/v2/source-corrections/candidates?limit=50'),
      ])
      setSourceExceptions(zkt)
      setHikvisionCandidates(hik.rows || [])
    } catch (error) {
      setSourceError(error)
    }
  }, [])

  const summaryRefresh = useThrottledRunner(loadSummary, RECOVERY_SUMMARY_REFRESH_MS)
  const sourceRefresh = useThrottledRunner(loadSourceExceptions, RECOVERY_SUMMARY_REFRESH_MS)
  const refreshNow = useCallback(() => {
    void summaryRefresh.runNow()
    void sourceRefresh.runNow()
  }, [sourceRefresh, summaryRefresh])

  // First load and every explicit Refresh run at once (after any request in
  // flight); realtime revisions are throttled.
  useEffect(() => {
    refreshNow()
  }, [refreshNow, refreshToken])
  const revisionRef = useRef(revision)
  useEffect(() => {
    if (revisionRef.current === revision) return
    revisionRef.current = revision
    summaryRefresh.schedule()
    sourceRefresh.schedule()
  }, [revision, sourceRefresh, summaryRefresh])

  useEffect(() => {
    if (!job || ['COMPLETED', 'COMPLETED_WITH_ATTENTION', 'CANCELLED'].includes(job.status)) return
    const timer = window.setInterval(() => {
      void api<AttendanceRecoveryJob>(`/api/v2/attendance-recovery/jobs/${job.job_id}`).then(setJob).catch(() => undefined)
    }, 2500)
    return () => window.clearInterval(timer)
  }, [job])

  // Corrected times are typed in Pakistan time and sent as UTC.
  const correctionInputs = useMemo(() => [...selectedExceptions]
    .filter((id) => correctionTimes[id])
    .map((id) => ({ source_kind: 'ZKT_MANIFEST', source_ref: String(id), corrected_device_time: pktInputToUtc(correctionTimes[id]) }))
    .concat([...selectedHikvision]
      .filter((id) => hikvisionCorrectionTimes[id])
      .map((id) => ({ source_kind: 'HIKVISION_EVIDENCE', source_ref: String(id), corrected_device_time: pktInputToUtc(hikvisionCorrectionTimes[id]) }))),
  [correctionTimes, hikvisionCorrectionTimes, selectedExceptions, selectedHikvision])

  const previewCorrections = async () => {
    if (!correctionInputs.length) return
    setBusy(true)
    setActionError('')
    try {
      const result = await api<CorrectionPreview>('/api/v2/source-corrections/preview', {
        method: 'POST',
        body: JSON.stringify({ corrections: correctionInputs, reason: reason.trim() || 'Preview timestamp correction.' }),
      })
      setCorrectionPreview(result)
      setConfirmation(result.confirmation)
    } catch (error) {
      setActionError(error instanceof Error && error.message ? error.message : "Couldn't show the fixes. Try again.")
    } finally {
      setBusy(false)
    }
  }

  const createCorrections = async () => {
    if (!correctionPreview) return
    setBusy(true)
    setActionError('')
    try {
      const result = await api<AttendanceRecoveryJob>('/api/v2/source-corrections/jobs', {
        method: 'POST',
        body: JSON.stringify({ corrections: correctionInputs, reason, password, typed_confirmation: confirmation, candidate_digest: correctionPreview.candidate_digest, preview_expires_at: correctionPreview.preview_expires_at, preview_signature: correctionPreview.preview_signature, idempotency_key: idempotency('attendance-source-correction') }),
      })
      setJob(result)
      toast.notice(FIX_TIMES.queued)
    } catch (error) {
      setActionError(error instanceof Error && error.message ? error.message : "Couldn't fix the times. Try again.")
    } finally {
      setBusy(false)
    }
  }

  const toggle = (setter: typeof setSelectedExceptions, id: number) => setter((current) => {
    const next = new Set(current)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  const controlJob = async (control: 'pause' | 'resume' | 'cancel' | 'retry') => {
    if (!job || !password || reason.trim().length < 10) return
    const expectedConfirmation = `${control.toUpperCase()} ${job.job_id}`
    if (controlConfirmation.trim() !== expectedConfirmation) {
      setActionError(`Type ${expectedConfirmation} to confirm.`)
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
      toast.notice(FIX_TIMES.controlDone[control])
    } catch (error) {
      setActionError(error instanceof Error && error.message ? error.message : "That didn't work. Try again.")
    } finally {
      setControlBusy(false)
    }
  }

  const delivery = summary?.delivery
  const summaryAsOf = formatMetricsTime(summary?.metrics_computed_at)
  const number = (value: number | undefined) => (summary ? value ?? 0 : null)
  const correctionEnabled = Boolean(summary?.correction_enabled)
  const toFix = (sourceExceptions?.totals?.open ?? 0) + hikvisionCandidates.length
  const jobWords = job ? recoveryJob(job.status) : null
  const controlPhrase = job ? `${['QUEUED', 'RUNNING', 'PAUSE_REQUESTED'].includes(job.status) ? 'PAUSE' : job.status === 'PAUSED' ? 'RESUME' : 'CANCEL'} ${job.job_id}` : ''

  return (
    <div className="recover-held">
      <section className="recover-section-head" aria-labelledby="recover-held-title">
        <h2 id="recover-held-title">{HELD_TAB.title}</h2>
        <p>{HELD_TAB.intro}</p>
        <p className="recover-muted">
          {HELD_TAB.introMore}{summaryAsOf ? ` ${HELD_TAB.figuresFrom(summaryAsOf)}.` : ''} {HELD_TAB.national}
        </p>
      </section>
      {Boolean(summaryError) && (
        <ErrorState compact title={HELD_TAB.loadFailed} message={summary ? 'Showing the last figures we had.' : undefined} error={summaryError} onRetry={() => summaryRefresh.runNow()} />
      )}
      <ul className="recover-held-rows" aria-label="Where the check-ins are">
        <SummaryRow label={HELD_TAB.retry} value={number(delivery?.safe_retryable)} sentence={HELD_TAB.retryText} />
        <SummaryRow label={HELD_TAB.sending} value={number(delivery?.active_in_flight)} />
        <SummaryRow label={HELD_TAB.missing} value={number(delivery?.missing_outbox)} sentence={notLinedUpText(delivery)} tone={notLinedUpTone(delivery)} />
        <SummaryRow label={HELD_TAB.unmatched} value={number(delivery?.identity_held)} link={{ label: HELD_TAB.unmatchedLink, href: '/attendance?view=fix-tools&tool=auto-send' }} />
        <SummaryRow label={HELD_TAB.setAside} value={number(delivery?.permanent_review)} sentence={setAsideText(summary?.source?.zkt_invalid_time ?? 0)} tone={delivery?.permanent_review ? 'warning' : 'neutral'} link={onShowUnreadable ? { label: HELD_TAB.setAsideLink, onClick: onShowUnreadable } : { label: HELD_TAB.setAsideLink, href: '/recover?tab=unreadable' }} />
      </ul>

      {!summary && !summaryError && <Skeleton variant="block" label="Loading" />}
      {summary && !correctionEnabled && !job && (
        <p className="recover-muted recover-fix-off">{FIX_TIMES.off}</p>
      )}
      {summary && (correctionEnabled || job) && (
        <section className="recover-fix panel" aria-labelledby="recover-fix-title">
          <div className="recover-section-head">
            <h2 id="recover-fix-title">{FIX_TIMES.title}</h2>
            <p>{FIX_TIMES.intro}</p>
            <p className="recover-muted">{FIX_TIMES.toFix(toFix)}</p>
          </div>
          {Boolean(sourceError) && <ErrorState compact title="Couldn't load the records to fix." error={sourceError} onRetry={() => sourceRefresh.runNow()} />}
          {actionError && <Callout tone="danger" role="alert" title="That didn't work.">{actionError}</Callout>}
          <h3 className="recover-fix-heading" data-brand>{FIX_TIMES.zkt}</h3>
          <ul className="recover-fix-list">
            {sourceExceptions?.rows?.map((row) => {
              const record = row.ordinal.toLocaleString()
              return (
                <li key={`zkt-${row.id}`}>
                  <label className="recover-bulk-check">
                    <input type="checkbox" checked={selectedExceptions.has(row.id)} onChange={() => toggle(setSelectedExceptions, row.id)} />
                    <span><strong data-device-code>{row.display_name || 'Device'}</strong><small>{`Record ${record}`}</small></span>
                  </label>
                  <label className="recover-fix-time">
                    <span>{FIX_TIMES.timeLabel(record)}</span>
                    <input type="datetime-local" disabled={!selectedExceptions.has(row.id)} value={correctionTimes[row.id] || ''} onChange={(event) => setCorrectionTimes((current) => ({ ...current, [row.id]: event.target.value }))} />
                  </label>
                  <TechnicalDetails><p>{row.error_code || 'INVALID_TIME'} · UID {row.observed_uid || 'unparsed'} · source {row.raw_record_digest.slice(0, 12)}… · {row.terminal_serial}</p></TechnicalDetails>
                </li>
              )
            })}
          </ul>
          <h3 className="recover-fix-heading" data-brand>{FIX_TIMES.hik}</h3>
          <ul className="recover-fix-list">
            {hikvisionCandidates.map((row) => (
              <li key={`hik-${row.id}`}>
                <label className="recover-bulk-check">
                  <input type="checkbox" disabled={!row.eligible} checked={selectedHikvision.has(row.id)} onChange={() => toggle(setSelectedHikvision, row.id)} />
                  <span><strong data-device-code>{row.display_name || 'Device'}</strong><small>{row.eligible ? FIX_TIMES.allowed : FIX_TIMES.notAllowed}</small></span>
                </label>
                <label className="recover-fix-time">
                  <span>{FIX_TIMES.timeLabel(String(row.id))}</span>
                  <input type="datetime-local" disabled={!row.eligible || !selectedHikvision.has(row.id)} value={hikvisionCorrectionTimes[row.id] || ''} onChange={(event) => setHikvisionCorrectionTimes((current) => ({ ...current, [row.id]: event.target.value }))} />
                </label>
                <TechnicalDetails><p>{row.terminal_serial} · epoch {row.source_epoch} · {row.disposition} · digest {row.observation_sha256.slice(0, 12)}…</p></TechnicalDetails>
              </li>
            ))}
          </ul>
          {sourceExceptions && !sourceExceptions.rows?.length && !hikvisionCandidates.length && <p className="recover-muted">{FIX_TIMES.none}</p>}
          <div className="recover-form">
            <label>
              <span>{FIX_TIMES.note}</span>
              <textarea value={reason} onChange={(event) => setReason(event.target.value)} minLength={10} placeholder="At least 10 letters" />
            </label>
            <label>
              <span>{FIX_TIMES.password}</span>
              <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" />
            </label>
          </div>
          <div className="recover-fix-actions">
            <Button busy={busy && !correctionPreview} busyLabel="Checking…" disabled={busy || !correctionInputs.length} onClick={() => previewCorrections()}>{FIX_TIMES.show}</Button>
          </div>
          {correctionPreview && (
            <div className="recover-fix-preview" aria-live="polite">
              <p><strong>{`${formatCount(correctionPreview.counts.eligible)} can be fixed`}</strong> · {`${formatCount(correctionPreview.counts.rejected)} can't`}</p>
              <label>
                <span>{FIX_TIMES.typePhrase(correctionPreview.confirmation)}</span>
                <input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" data-technical />
              </label>
              <Button variant="primary" busy={busy} busyLabel="Saving…" disabled={busy || !password || confirmation !== correctionPreview.confirmation || !summary?.execution_enabled || !summary?.correction_enabled} onClick={() => createCorrections()}>
                {FIX_TIMES.apply(correctionPreview.counts.eligible)}
              </Button>
              {!summary?.correction_enabled && <p className="recover-muted">{FIX_TIMES.off}</p>}
              {!!correctionPreview.rejected?.length && (
                <TechnicalDetails>
                  {correctionPreview.rejected.map((item) => <p key={item.source_ref}>{item.source_ref}: {item.reason}</p>)}
                </TechnicalDetails>
              )}
            </div>
          )}
          {job && jobWords && (
            <article className="recover-fix-job" aria-label={FIX_TIMES.batch}>
              <header>
                <h3>{FIX_TIMES.batch}</h3>
                <StatusChip tone={chipTone(jobWords.tone)} label={jobWords.label} code={job.status} />
              </header>
              <p>{FIX_TIMES.progress(job.progress.succeeded, job.progress.failed, job.progress.skipped, job.progress.requested)}</p>
              <p className="recover-muted">{`Updated ${ago(job.updated_at)}`}</p>
              <label className="recover-fix-phrase">
                <span>{FIX_TIMES.typePhrase(controlPhrase)}</span>
                <input value={controlConfirmation} onChange={(event) => setControlConfirmation(event.target.value)} autoComplete="off" data-technical />
                <small>Use the action word followed by the full job ID. Note and password above are needed too.</small>
              </label>
              <div className="recover-fix-actions">
                {['QUEUED', 'RUNNING', 'PAUSE_REQUESTED'].includes(job.status) && <Button busy={controlBusy} busyLabel="Pausing…" disabled={controlBusy || controlConfirmation.trim() !== `PAUSE ${job.job_id}`} onClick={() => controlJob('pause')}>Pause</Button>}
                {job.status === 'PAUSED' && <Button busy={controlBusy} busyLabel="Resuming…" disabled={controlBusy || controlConfirmation.trim() !== `RESUME ${job.job_id}`} onClick={() => controlJob('resume')}>Resume</Button>}
                {job.status === 'COMPLETED_WITH_ATTENTION' && <Button busy={controlBusy} busyLabel="Trying again…" disabled={controlBusy || controlConfirmation.trim() !== `RETRY ${job.job_id}`} onClick={() => controlJob('retry')}>Try the failed ones again</Button>}
                {!['COMPLETED', 'CANCELLED'].includes(job.status) && <Button variant="danger" busy={controlBusy} busyLabel="Stopping…" disabled={controlBusy || controlConfirmation.trim() !== `CANCEL ${job.job_id}`} onClick={() => controlJob('cancel')}>Stop the rest</Button>}
              </div>
              <TechnicalDetails>
                <p>{job.job_id} · {job.action} · {job.status} · started {job.started_at ? dayTime(job.started_at) : 'not yet'}</p>
                {job.items?.filter((item) => item.status !== 'SUCCEEDED').slice(0, 50).map((item) => (
                  <p key={item.item_id}>{item.source_ref} · {item.status} · {item.outcome || 'pending'} · {item.error_code || 'Awaiting worker'} {item.error_message || ''}</p>
                ))}
              </TechnicalDetails>
            </article>
          )}
        </section>
      )}
    </div>
  )
}
