// Recovery details (08-advanced 1.4): a sheet with the card's status, what to
// do, how far it got and what happened, in plain words. Every engineering
// field (serial, epoch, record number, chain digest, proofs, the technical
// record download and records that changed on the machine) stays under a
// closed Technical details.
import { useCallback, useEffect, useMemo, useState, type RefObject } from 'react'
import { api } from '../../api'
import { Button, ErrorState, Sheet, Skeleton, StatusChip, TechnicalDetails, Timeline, type TimelineItem } from '../../ui'
import { dayTime } from '../../lib/time'
import { formatCount } from '../../words/glossary'
import { hrCountsLine, readCountsLine, recoverEvent } from '../../words/recover'
import type { ReconciliationJob } from '../../types'
import type { Place } from './names'
import { ProgressBar, RecoveryStages, capturePercent, cardWords, hrProgress } from './RecoveryCard'
import { chipTone } from './tones'

const PAGE = 25

const eventLine = (details: Record<string, unknown>) => {
  const start = Number(details.start_ordinal)
  const end = Number(details.end_ordinal)
  if (Number.isFinite(start) && Number.isFinite(end) && end >= start) return `${formatCount(end - start)} records saved`
  return ''
}

export function RecoveryDetails({
  seed,
  place,
  onClose,
  onReview,
  onRecordChanged,
  returnFocusRef,
}: {
  seed: ReconciliationJob
  place: Place
  onClose: () => void
  onReview: (job: ReconciliationJob) => void
  onRecordChanged: (divergenceId: string) => void
  returnFocusRef?: RefObject<HTMLElement | null>
}) {
  const [job, setJob] = useState<ReconciliationJob | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [shown, setShown] = useState(PAGE)
  const load = useCallback(async () => {
    setError(null)
    try {
      setJob(await api<ReconciliationJob>(`/api/v1/reconciliations/${seed.job_id}`))
      setShown(PAGE)
    } catch (reason) {
      setError(reason)
    }
  }, [seed.job_id])
  useEffect(() => { void load() }, [load])

  const current = job || seed
  const words = cardWords(current)
  const events = useMemo(() => (job?.events || []).slice().reverse(), [job?.events])
  const items: TimelineItem[] = events.slice(0, shown).map((event, index) => {
    const word = recoverEvent(event.state)
    const extra = eventLine(event.details || {})
    return {
      id: `${event.created_at}-${index}`,
      time: dayTime(event.created_at),
      dateTime: event.created_at,
      title: word.unknown ? 'Step recorded' : word.label,
      description: extra || undefined,
      technical: <><p>{event.state}</p><pre>{JSON.stringify(event.details, null, 2)}</pre></>,
    }
  })
  const reviewAction = words.action === 'review' || words.action === 'records'
  const capture = capturePercent(current)
  const hr = hrProgress(current)
  const readLine = readCountsLine(current.progress)
  const hrLine = hrCountsLine(current.progress)

  return (
    <Sheet
      title="Recovery details"
      className="recover-sheet recover-details-sheet"
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      description={`${place.name} · ${place.division === 'life' ? 'Life' : 'Health'} · started ${dayTime(current.requested_at)}`}
      footer={reviewAction ? <Button variant="primary" fullWidth onClick={() => onReview(current)}>{words.actionLabel}</Button> : undefined}
    >
      <section className="recover-detail-block" aria-label="Status">
        <StatusChip tone={chipTone(words.tone)} icon={words.tone === 'problem' ? 'alert' : undefined} label={words.chip} code={current.operator_state || current.status} />
        <p>{words.sentence}</p>
      </section>
      {words.action && words.action !== 'details' && (
        <section className="recover-detail-block">
          <h3>What to do</h3>
          <p>{reviewAction ? `${words.actionLabel}: the button at the bottom opens them.` : words.action === 'attendance' ? 'Open Attendance with the Not sent filter.' : words.action === 'archive' ? 'Nothing. You can hide it from the list.' : `Use ${words.actionLabel} on the card if you need to.`}</p>
        </section>
      )}
      <section className="recover-detail-block">
        <h3>How far it got</h3>
        <RecoveryStages words={words} />
        <div className="recover-card-bars">
          <ProgressBar label="Read from the machine" percent={capture} />
          <ProgressBar label="Reached the HR system" percent={hr.percent} text={hr.target === 0 && !current.oracle_certified_at ? 'Nothing to send yet' : undefined} />
        </div>
        {(readLine || hrLine) && <p className="recover-card-counts">{[readLine, hrLine].filter(Boolean).join(' · ')}</p>}
      </section>
      <section className="recover-detail-block">
        <h3>What happened</h3>
        <p className="recover-muted">Newest first</p>
        {!job && !error && <Skeleton variant="row" count={3} label="Loading what happened" />}
        {Boolean(error) && <ErrorState compact title="Couldn't load what happened." error={error} onRetry={load} />}
        {job && !events.length && <p className="recover-muted">Nothing recorded yet.</p>}
        {items.length > 0 && <Timeline items={items} label="What happened" className="recover-events" />}
        {shown < events.length && (
          <div className="recover-more">
            <Button onClick={() => setShown((count) => Math.min(count + PAGE, events.length))}>Show 25 more</Button>
            <small>{`Showing ${formatCount(Math.min(shown, events.length))} of ${formatCount(events.length)}`}</small>
          </div>
        )}
      </section>
      <TechnicalDetails>
        <dl className="recover-tech">
          <div><dt>Recovery</dt><dd>{current.job_id}</dd></div>
          <div><dt>Machine serial number</dt><dd>{current.terminal.serial || 'not known yet'}</dd></div>
          <div><dt>Source epoch</dt><dd>{current.mode === 'HIKVISION_SERIAL_HISTORY' ? current.recovery?.source_epoch_id || 'Pending' : current.recovery?.source_epoch ?? 1}</dd></div>
          <div><dt>{current.mode === 'HIKVISION_SERIAL_HISTORY' ? 'Committed source serial' : 'Committed next ordinal'}</dt><dd>{(current.checkpoint?.source_serial ?? current.checkpoint?.next_ordinal)?.toLocaleString() ?? '—'}</dd></div>
          <div><dt>Chain digest</dt><dd><code>{current.checkpoint?.chain_digest || 'Not available'}</code></dd></div>
          <div><dt>Link box working on it</dt><dd><code>{current.assignment.assignment_id || 'None'}</code></dd></div>
          <div><dt>Last link box update</dt><dd>{current.assignment.heartbeat_at ? dayTime(current.assignment.heartbeat_at) : 'Not active'}</dd></div>
          <div><dt>Machine read proof</dt><dd>{current.capture_certified_at ? dayTime(current.capture_certified_at) : 'Pending'}</dd></div>
          <div><dt>HR system proof</dt><dd>{current.oracle_certified_at ? dayTime(current.oracle_certified_at) : 'Pending'}</dd></div>
          {current.operator_message && <div><dt>Server message</dt><dd>{current.operator_message}</dd></div>}
          {(current.error_code || current.error_message) && <div><dt>Problem</dt><dd>{current.error_code || '—'}{current.error_message ? `: ${current.error_message}` : ''}</dd></div>}
        </dl>
        <details className="recover-json">
          <summary>Machine read proof (JSON)</summary>
          <pre>{JSON.stringify(current.capture_certificate || { state: 'Pending' }, null, 2)}</pre>
        </details>
        <details className="recover-json">
          <summary>HR system proof (JSON)</summary>
          <pre>{JSON.stringify(current.oracle_certificate || { state: 'Pending' }, null, 2)}</pre>
        </details>
        <p><a className="button secondary" href={`/api/v1/reconciliations/${current.job_id}/evidence`} target="_blank" rel="noreferrer">Download technical record (JSON)</a></p>
        {current.recovery?.divergence && (
          <div className="recover-changed">
            <p><strong>Records that changed on the machine</strong></p>
            <p>Record {current.recovery.divergence.ordinal.toLocaleString()} was read {current.recovery.divergence.observation_count} times and looked different.</p>
            <Button onClick={() => onRecordChanged(current.recovery?.divergence?.divergence_id || '')}>
              {`Record ${current.recovery.divergence.ordinal.toLocaleString()} changed on the machine`}
            </Button>
          </div>
        )}
      </TechnicalDetails>
    </Sheet>
  )
}

