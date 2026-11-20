// Removing several people: progress bar, person-by-person list, Stop (password
// kept) and Clear (07-people P8). Kept out of the lazy dialogs chunk because the
// page shows it without opening a dialog.
import { useState, type ReactNode } from 'react'
import type { UserDeletionJob } from '../../types'
import { TechnicalDetails } from '../../ui'
import { Icon } from '../../Icon'
import { plainStatus } from '../../words/states'
import { requestFailure } from '../../words/requests'
import { REMOVE_MANY, peopleError, removalHeading, removalItem } from '../../words/people'

const ACTIVE_JOB = ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED']

const itemReason = (item: UserDeletionJob['items'][number]) => {
  if (item.error_code) {
    const coded = requestFailure(item.error_code)
    if (!coded.unknown) return coded.sentence.replace(/\.$/, '')
  }
  return peopleError(item.error_message)?.replace(/\.$/, '') || null
}
export const isActiveRemoval = (job: UserDeletionJob | null | undefined) => Boolean(job && ACTIVE_JOB.includes(job.status))

/** Removing several people: progress bar, person-by-person list, Stop (password kept) and Clear. */
export function BulkDeletionProgress({
  job,
  deviceName,
  onCancel,
  onClear,
}: {
  job: UserDeletionJob
  deviceName?: string
  onCancel: (password: string) => Promise<void>
  onClear?: () => void
}) {
  const active = isActiveRemoval(job)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const done = job.counts.succeeded + job.counts.failed + job.counts.canceled + job.counts.expired
  const total = Math.max(job.counts.requested, job.items.length, 1)
  const word = plainStatus('peopleRemoval', job.status)
  const items: ReactNode = job.items.length > 0 && (
    <ul className="people-removal-items">
      {job.items.map((item) => {
        const status = item.status.toUpperCase()
        const icon = status === 'SUCCEEDED' ? 'check' : status === 'FAILED' ? 'x' : status === 'CANCELED' || status === 'CANCELLED' || status === 'EXPIRED' ? 'pause' : 'clock'
        return (
          <li key={item.user_key} data-item-status={status}>
            <Icon name={icon} size="sm" />
            <span><span data-user-text>{item.display_name}</span> — {removalItem(status, status === 'FAILED' ? itemReason(item) : null)}</span>
            {(item.error_code || item.error_message) && <TechnicalDetails>{[item.user_id, item.error_code, item.error_message].filter(Boolean).join(' · ')}</TechnicalDetails>}
          </li>
        )
      })}
    </ul>
  )
  return (
    <section className={`people-removal-card is-${word.tone}`} aria-live="polite" aria-label={REMOVE_MANY.progressTitle(deviceName || '')}>
      <p className="people-removal-title">{REMOVE_MANY.progressTitle(deviceName || '')}</p>
      <p className="people-removal-heading">{removalHeading(job.status, job.counts)}</p>
      <div className="people-removal-bar" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done} aria-label={REMOVE_MANY.progressDone(done, total)}>
        <span style={{ width: `${Math.round((done / total) * 100)}%` }} />
      </div>
      <p className="people-muted">{REMOVE_MANY.progressCounts(job.counts.succeeded, job.counts.pending, job.counts.failed, job.counts.canceled + job.counts.expired)}</p>
      {items}
      <div className="people-removal-actions">
        {active && job.status !== 'CANCEL_REQUESTED' && (
          !cancelOpen
            ? <button type="button" className="button secondary small" onClick={() => setCancelOpen(true)}>{REMOVE_MANY.stop}</button>
            : (
              <form className="people-removal-stop" onSubmit={async (event) => {
                event.preventDefault()
                if (!password || busy) return
                setBusy(true)
                try { await onCancel(password) } catch { /* the page shows the error */ } finally { setBusy(false) }
              }}>
                <label><span className="sr-only">{REMOVE_MANY.stopPassword}</span><input aria-label={REMOVE_MANY.stopPassword} type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder={REMOVE_MANY.stopPassword} /></label>
                <button className="button destructive small" type="submit" disabled={busy || !password}>{busy ? REMOVE_MANY.stopping : REMOVE_MANY.stopConfirm}</button>
              </form>
            )
        )}
        {!active && onClear && <button type="button" className="button secondary small" onClick={onClear}>{REMOVE_MANY.clearCard}</button>}
      </div>
      <TechnicalDetails>{[job.job_id, job.status].join(' · ')}</TechnicalDetails>
    </section>
  )
}
