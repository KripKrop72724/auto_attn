// One recovery as a short card (08-advanced 1.3): place + Life/Health tag, one
// chip, one sentence, the stage strip, two plain progress bars and one main
// button. Stop is the visible second button; Details, Stop and hide, Hide and
// (when it still makes sense) Try again sit in ⋯. Every action stays one click.
import { useRef } from 'react'
import { ActionMenu, Button, DivisionTag, StatusChip, TechnicalDetails, type ActionMenuItem } from '../../ui'
import { ago, dayTime } from '../../lib/time'
import { formatCount } from '../../words/glossary'
import {
  RECOVER_STAGES,
  hrCountsLine,
  readCountsLine,
  recoveryCard,
  recoveryEta,
  scopeCardLine,
  sourceStripText,
  doneSentence,
  type CardAction,
  type RecoveryCardWords,
  type StageState,
} from '../../words/recover'
import type { ReconciliationJob } from '../../types'
import type { Place } from './names'
import { chipTone } from './tones'

export type JobControl = 'pause' | 'resume' | 'cancel' | 'retry'
export type JobAction = JobControl | 'archive' | 'cancel-and-archive'

const terminalStates = new Set(['COMPLETED', 'FAILED', 'CANCELLED', 'INVALIDATED'])

export const BUSY_WORDS: Record<JobAction, string> = {
  pause: 'Pausing…',
  resume: 'Resuming…',
  retry: 'Trying again…',
  cancel: 'Stopping…',
  'cancel-and-archive': 'Hiding…',
  archive: 'Hiding…',
}

/** Which controls the server allows for this job (the old card's rules, unchanged). */
export function jobControls(job: ReconciliationJob) {
  const gate = job.source_exception_assurance?.state
  const gateActive = gate === 'REVIEW_REQUIRED' || gate === 'IDENTITY_EVIDENCE_REQUIRED' || gate === 'INTERPRETATION_REQUIRED' || gate === 'NEWEST_PUNCH_UNRECEIVED' || gate === 'SCOPE_MISMATCH'
  return {
    pause: ['QUEUED', 'RUNNING', 'PAUSE_REQUESTED'].includes(job.status),
    resume: job.status === 'PAUSED',
    retry: job.status === 'NEEDS_ATTENTION' && !gateActive,
    cancel: !terminalStates.has(job.status),
  }
}

/** Percent read from the machine (the existing rule). */
export function capturePercent(job: ReconciliationJob) {
  const cutoff = job.terminal.cutoff_count || 0
  if (job.scope === 'RECENT_TAIL') return job.recent_tail?.caught_up ? 100 : 0
  if (job.scope === 'ACTIVE_USERS' && job.active_user_scope?.user_count) {
    return job.capture_certified_at ? 100 : Math.min(99, Math.round(100 * job.active_user_scope.completed_users / job.active_user_scope.user_count))
  }
  if (cutoff) return Math.min(100, Math.round((job.progress.scanned / cutoff) * 100))
  return job.capture_certified_at ? 100 : 0
}

/** Percent that reached the HR system; never 100 while records still can't be read. */
export function hrProgress(job: ReconciliationJob): { percent: number; target: number } {
  const progress = job.progress
  const target = Math.max(progress.oracle_target || 0, progress.oracle_confirmed + progress.oracle_pending + (progress.oracle_review_required || 0) + (progress.blocked_identity || 0))
  const rawPending = job.source_exception_assurance?.raw_interpretation_open ?? 0
  const percent = target
    ? Math.min(rawPending ? 99 : 100, progress.oracle_confirmed < target ? Math.min(99, Math.floor((progress.oracle_confirmed / target) * 100)) : 100)
    : job.oracle_certified_at ? (rawPending ? 99 : 100) : 0
  return { percent, target }
}

export function cardWords(job: ReconciliationJob, queuePosition?: number, running?: number): RecoveryCardWords {
  return recoveryCard(job, { queuePosition, running })
}

const stageMark = (index: number, current: number, state: StageState) => {
  if (state === 'done') return 'done'
  if (index < current) return 'done'
  if (index > current) return 'pending'
  return state === 'failed' ? 'failed' : state === 'paused' ? 'paused' : state === 'stopped' ? 'stopped' : state === 'unknown' ? 'unknown' : 'active'
}

const markWords: Record<string, string> = { done: 'done', pending: 'not reached yet', failed: 'stopped here, waiting for you', paused: 'paused here', stopped: 'stopped here', unknown: 'not known', active: 'now' }

export function RecoveryStages({ words, percent }: { words: RecoveryCardWords; percent?: number }) {
  const current = RECOVER_STAGES.findIndex((stage) => stage.id === words.stage)
  const currentStage = RECOVER_STAGES[Math.max(0, current)]
  return (
    <div className="recover-stages">
      <ol className="recover-stage-strip" aria-label="How far it got">
        {RECOVER_STAGES.map((stage, index) => {
          const mark = stageMark(index, current, words.stageState)
          return (
            <li key={stage.id} data-mark={mark} aria-current={index === current ? 'step' : undefined}>
              <span className="recover-stage-dot" aria-hidden="true">{mark === 'done' ? '✓' : mark === 'failed' ? '✕' : ''}</span>
              <span className="recover-stage-label">{stage.label}</span>
              <span className="sr-only"> ({markWords[mark]})</span>
            </li>
          )
        })}
      </ol>
      <p className="recover-stage-compact" aria-hidden="true">
        {currentStage.short}{percent !== undefined && words.stage !== 'done' ? ` · ${percent}%` : ''}
      </p>
    </div>
  )
}

export function ProgressBar({ label, percent, text }: { label: string; percent: number; text?: string }) {
  return (
    <div className="recover-bar">
      <div className="recover-bar-head"><span>{label}</span><strong>{text ?? `${percent}%`}</strong></div>
      <div className="recover-bar-track" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-valuetext={text}>
        <i style={{ width: `${percent}%` }} />
      </div>
    </div>
  )
}

export interface RecoveryCardProps {
  job: ReconciliationJob
  place: Place
  queuePosition?: number
  running?: number
  busy: ReadonlySet<string>
  highlighted?: boolean
  onControl: (job: ReconciliationJob, action: JobControl) => unknown
  onArchive: (job: ReconciliationJob, cancelFirst: boolean) => unknown
  onReview: (job: ReconciliationJob) => void
  onDetails: (job: ReconciliationJob, trigger: HTMLButtonElement | null) => void
  onShowHeld: () => void
}

export function RecoveryCard({ job, place, queuePosition, running, busy, highlighted, onControl, onArchive, onReview, onDetails, onShowHeld }: RecoveryCardProps) {
  const detailsRef = useRef<HTMLButtonElement>(null)
  const words = cardWords(job, queuePosition, running)
  const controls = jobControls(job)
  const isBusy = (action: JobAction) => busy.has(`${job.job_id}:${action}`)
  const cardBusy = [...busy].some((key) => key.startsWith(`${job.job_id}:`))
  const capture = capturePercent(job)
  const hr = hrProgress(job)
  const nothingToSend = hr.target === 0 && !job.oracle_certified_at
  const assurance = job.source_exception_assurance
  const strip = assurance && assurance.total > 0 ? sourceStripText(assurance.state, job.status, assurance.open) : null
  const readLine = readCountsLine(job.progress)
  const hrLine = hrCountsLine(job.progress)
  const done = words.stage === 'done' ? doneSentence(job.progress) : null
  const retries = job.progress.auto_retry_count ?? 0
  const scope = job.mode === 'HIKVISION_SERIAL_HISTORY' ? scopeCardLine(job) : 'Everything on the machine'
  const showDetails = () => onDetails(job, detailsRef.current)

  const run = (action: CardAction) => {
    switch (action) {
      case 'pause': return controls.pause ? onControl(job, 'pause') : undefined
      case 'resume': return controls.resume ? onControl(job, 'resume') : undefined
      case 'retry': return controls.retry ? onControl(job, 'retry') : undefined
      case 'cancel': return controls.cancel ? onControl(job, 'cancel') : undefined
      case 'archive': return onArchive(job, false)
      case 'review':
      case 'records': return onReview(job)
      case 'details': return showDetails()
      default: return undefined
    }
  }
  const primaryAllowed = words.action && (
    words.action === 'pause' ? controls.pause
      : words.action === 'resume' ? controls.resume
        : words.action === 'retry' ? controls.retry && !words.noRetry
          : words.action === 'cancel' ? controls.cancel
            : words.action === 'archive' ? !controls.cancel
              : true)
  const primaryBusyKey: JobAction | null = words.action === 'pause' || words.action === 'resume' || words.action === 'retry' || words.action === 'cancel' ? words.action
    : words.action === 'archive' ? 'archive' : null
  const menu: ActionMenuItem[] = []
  if (words.action !== 'details') menu.push({ id: 'details', label: 'Details', onSelect: showDetails })
  if (controls.retry && !words.noRetry && words.action !== 'retry') menu.push({ id: 'retry', label: isBusy('retry') ? BUSY_WORDS.retry : 'Try again', disabled: cardBusy, onSelect: () => void onControl(job, 'retry') })
  if (controls.cancel) menu.push({ id: 'cancel-and-archive', label: isBusy('cancel-and-archive') ? BUSY_WORDS['cancel-and-archive'] : 'Stop and hide', disabled: cardBusy, onSelect: () => void onArchive(job, true) })
  else if (words.action !== 'archive') menu.push({ id: 'archive', label: isBusy('archive') ? BUSY_WORDS.archive : 'Hide', disabled: cardBusy, onSelect: () => void onArchive(job, false) })

  return (
    <article
      className="recover-card"
      data-job-id={job.job_id}
      data-tone={words.tone}
      data-highlight={highlighted ? '' : undefined}
      aria-labelledby={`recover-card-${job.job_id}`}
    >
      <header className="recover-card-head">
        <div className="recover-card-title">
          <h3 id={`recover-card-${job.job_id}`}>{place.name}</h3>
          <DivisionTag division={place.division} />
        </div>
        <StatusChip tone={chipTone(words.tone)} icon={words.tone === 'problem' ? 'alert' : undefined} label={words.chip} code={job.operator_state || job.status} />
      </header>
      <p className="recover-card-sentence">{done || words.sentence}</p>
      {strip && <p className="recover-card-strip">{strip}</p>}
      <RecoveryStages words={words} percent={words.stage === 'checking' ? hr.percent : capture} />
      <div className="recover-card-bars">
        <ProgressBar label={job.scope === 'ACTIVE_USERS' ? 'People done' : 'Read from the machine'} percent={capture} />
        <ProgressBar label="Reached the HR system" percent={hr.percent} text={nothingToSend ? 'Nothing to send yet' : undefined} />
      </div>
      {(readLine || hrLine) && (
        <p className="recover-card-counts">
          {readLine}{readLine && hrLine ? ' · ' : ''}{hrLine}
        </p>
      )}
      <p className="recover-card-meta">
        {[scope, `Started ${dayTime(job.requested_at)}`, `Updated ${ago(job.updated_at)}`, recoveryEta(job), retries > 0 ? `Automatic retries: ${formatCount(retries)}` : ''].filter(Boolean).join(' · ')}
      </p>
      <p className="recover-card-note">
        Counts this recovery only. <button type="button" className="text-button" onClick={onShowHeld}>See everything not in the HR system yet</button>
      </p>
      <footer className="recover-card-actions">
        {words.action === 'attendance' ? (
          <a className="button primary" href="/attendance?status=problem">{words.actionLabel}</a>
        ) : primaryAllowed && words.action ? (
          <Button
            ref={words.action === 'details' ? detailsRef : undefined}
            variant={words.action === 'archive' ? 'secondary' : 'primary'}
            busy={primaryBusyKey ? isBusy(primaryBusyKey) : false}
            busyLabel={primaryBusyKey ? BUSY_WORDS[primaryBusyKey] : undefined}
            disabled={cardBusy && !(primaryBusyKey && isBusy(primaryBusyKey))}
            onClick={() => run(words.action as CardAction)}
          >
            {words.actionLabel}
          </Button>
        ) : null}
        {controls.cancel && words.action !== 'cancel' && (
          <Button variant="danger-outline" busy={isBusy('cancel')} busyLabel={BUSY_WORDS.cancel} disabled={cardBusy && !isBusy('cancel')} onClick={() => onControl(job, 'cancel')}>
            Stop
          </Button>
        )}
        <ActionMenu items={menu} label={`More actions for ${place.name}`} />
      </footer>
      <TechnicalDetails>
        <dl className="recover-tech">
          <div><dt>Recovery</dt><dd>{job.job_id}</dd></div>
          <div><dt>Device code</dt><dd>{place.code}</dd></div>
          <div><dt>Machine serial number</dt><dd>{job.terminal.serial || 'not known yet'}</dd></div>
          <div><dt>Records read so far</dt><dd>{(job.checkpoint?.source_serial ?? job.checkpoint?.next_ordinal ?? job.progress.scanned).toLocaleString()}</dd></div>
          <div><dt>Source epoch</dt><dd>{job.mode === 'HIKVISION_SERIAL_HISTORY' ? job.recovery?.source_epoch_id || 'Pending' : job.recovery?.source_epoch ?? 1}</dd></div>
          <div><dt>Link box working on it</dt><dd>{job.assignment.assignment_id || 'None'}</dd></div>
          <div><dt>Automatic retries</dt><dd>{retries}</dd></div>
          <div><dt>Status</dt><dd>{job.status} · {job.operator_state || '—'} · {job.phase} · {job.wait_reason || '—'}</dd></div>
          {job.operator_message && <div><dt>Server message</dt><dd>{job.operator_message}</dd></div>}
          {(job.error_code || job.error_message) && <div><dt>Problem</dt><dd>{job.error_code || '—'}{job.error_message ? `: ${job.error_message}` : ''}</dd></div>}
          {job.scope === 'RECENT_TAIL' && job.recent_tail && (
            <div><dt>Newest check-ins</dt><dd>{`Recent tail · from serial ${job.recent_tail.begin_serial ?? '?'} · last ${job.recent_tail.last_serial ?? 'pending'} · ${job.recent_tail.records ?? 0} new records after serial ${job.recent_tail.anchor_serial ?? '?'} · ${job.recent_tail.overlap_records ?? 0} held records re-read${job.recent_tail.fact_conflicts ? ` · ${job.recent_tail.fact_conflicts} changed serials held for review` : ''} · no coverage certificate`}</dd></div>
          )}
        </dl>
      </TechnicalDetails>
    </article>
  )
}
