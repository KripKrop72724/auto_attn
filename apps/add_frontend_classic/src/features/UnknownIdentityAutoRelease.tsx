import { useRef, useState, type FormEvent, type RefObject } from 'react'
import { api, ApiError } from '../api'
import { Dialog, dateTime, idempotency } from '../App'
import { Icon } from '../Icon'
import { humanizeStatus } from '../status'
import { explainOperatorError, OperatorFailureMessage, type OperatorFailure } from './AlertResolveDialog'
import {
  AUTO_RELEASE_PATH,
  elsewhereText,
  etaText,
  formatAgo,
  normalizeAutoReleaseStatus,
  punchCount,
  remainingOf,
  secondsSince,
  storeAutoReleaseStatus,
  useUnknownAutoReleaseStatus,
  type AutoReleaseStatus,
} from './unknownAutoRelease'
import './UnknownIdentityAutoRelease.css'

type Toast = { notice: (message: string) => void; error: (message: string) => void }

const REASON_MIN = 3
const REASON_MAX = 500

const errorText = (code: string, at: string) => {
  if (code === 'ADD_BUSY') return `ADD's database was busy ${at}; the next batch tries again by itself.`
  if (code === 'COUNT_BUSY') return `The backlog count was skipped ${at} because the database was busy; it is tried again in a minute, and less often while the database stays busy.`
  if (code === 'BATCH_FAILED') return `A batch failed ${at}; the releaser moved on and keeps going.`
  return `The last batch ${at} stopped with “${humanizeStatus(code)}”; the releaser moved on and keeps going.`
}

function stateBadge(status: AutoReleaseStatus) {
  if (!status.allowed) return { text: 'Blocked for this deployment', pattern: 'blocked' }
  if (status.enabled) return { text: status.backpressure ? 'ON · waiting for Oracle' : 'ON · releasing automatically', pattern: 'confirmed' }
  if (status.drain_active) return { text: 'OFF · Release all running', pattern: 'waiting' }
  return { text: 'OFF', pattern: 'waiting' }
}

function stateSentence(status: AutoReleaseStatus) {
  const when = status.changed_at ? ` since ${dateTime(status.changed_at)}` : ''
  const who = status.changed_by ? ` · turned ${status.enabled ? 'on' : 'off'} by ${status.changed_by}` : ''
  if (status.enabled) {
    return status.changed_by
      ? `ON${when}${who}. ADD releases these punches to Oracle by itself in small paced batches; nothing to click.`
      : 'ON by default. ADD releases these punches to Oracle by itself in small paced batches; nothing to click.'
  }
  if (status.drain_active) {
    return `OFF${when}${who}. Release all now was started${status.drain_requested_by ? ` by ${status.drain_requested_by}` : ''}${status.drain_requested_at ? ` at ${dateTime(status.drain_requested_at)}` : ''}; it stops by itself once every such punch has been tried.`
  }
  return `OFF${when}${who}. New punches of this kind stay held until it is turned back ON or someone clicks Release all now.`
}

/**
 * The owner's switch for "Unknown identity · CNIC on synced user" punches and
 * the progress of their release. ON (the default) the worker releases them in
 * small paced batches through the same Direct ORDS checks as a quick release;
 * OFF stops new batches only. Changing the switch needs the administrator's
 * password and a reason; "Release all now" (while OFF) is one click.
 */
export function UnknownIdentityAutoRelease({ toast, onChanged }: { toast?: Toast; onChanged?: () => void }) {
  const query = useUnknownAutoReleaseStatus(true)
  const status = query.data
  // What the dialog changes: the switch (ON/OFF) or stopping a running Release all while OFF.
  const [target, setTarget] = useState<boolean | null>(null)
  const [stopDrain, setStopDrain] = useState(false)
  const [starting, setStarting] = useState(false)
  const [failure, setFailure] = useState<OperatorFailure | null>(null)
  const switchRef = useRef<HTMLButtonElement>(null)
  const stopRef = useRef<HTMLButtonElement>(null)

  if (query.isPending) {
    return <p className="unknown-auto-release-loading" role="status">Loading automatic release…</p>
  }
  if (!status) {
    return query.isError
      ? <div className="message pattern-blocked" role="alert"><Icon name="alert" /><span>ADD could not load the automatic release status. It is retried every 10 seconds.</span></div>
      : null
  }

  const releaseAll = async () => {
    if (starting) return
    setStarting(true)
    setFailure(null)
    try {
      const value = normalizeAutoReleaseStatus(await api<unknown>(`${AUTO_RELEASE_PATH}/release-all`, {
        method: 'POST', body: JSON.stringify({ idempotency_key: idempotency('unknown-release-all') }),
      }))
      storeAutoReleaseStatus(value)
      const outcome = value?.outcome
      toast?.notice(outcome === 'ALREADY_AUTOMATIC'
        ? 'Automatic release is ON, so ADD is already releasing these punches.'
        : outcome === 'ALREADY_RUNNING'
          ? 'Release all is already running.'
          : 'Release all started. ADD releases every such punch in paced batches; progress shows here.')
      onChanged?.()
    } catch (error) {
      setFailure(explainOperatorError(error, 'ADD could not start Release all.'))
    } finally {
      setStarting(false)
    }
    void query.refetch()
  }

  const badge = stateBadge(status)
  const { progress, backlog } = status
  const remaining = remainingOf(status)
  const elsewhere = elsewhereText(status)
  const countedAgo = secondsSince(backlog.counted_at, status.server_time)
  const done = progress.confirmed_total
  const share = remaining === null || done + remaining === 0 ? null : Math.round((done / (done + remaining)) * 100)
  const errorAt = status.last_error_at ? formatAgo(secondsSince(status.last_error_at, status.server_time)) : 'recently'
  const openSwitch = (next: boolean, stopping = false) => { setStopDrain(stopping); setTarget(next) }

  return (
    <section id="unknown-identity-auto-release" className="unknown-auto-release" aria-labelledby="unknown-auto-release-title" aria-busy={query.isFetching}>
      <header className="unknown-auto-release-head">
        <div className="unknown-auto-release-title">
          <h3 id="unknown-auto-release-title">Automatic release to Oracle</h3>
          <p id="unknown-auto-release-state">{stateSentence(status)}</p>
          {status.reason && <p className="unknown-auto-release-reason"><span>Reason:</span> {status.reason}</p>}
        </div>
        <div className="unknown-auto-release-controls">
          <span className={`status-badge pattern-${badge.pattern}`}>{badge.text}</span>
          <button
            ref={switchRef}
            type="button"
            role="switch"
            aria-checked={status.enabled}
            aria-describedby="unknown-auto-release-state"
            className="auto-release-switch"
            onClick={() => openSwitch(!status.enabled)}
          >
            <span className="auto-release-switch-track" aria-hidden="true"><span /></span>
            <span>Automatic release</span>
            <strong aria-hidden="true">{status.enabled ? 'ON' : 'OFF'}</strong>
          </button>
          {!status.enabled && status.allowed && !status.drain_active && (
            <button className="button primary" type="button" disabled={starting} onClick={() => void releaseAll()}>
              <Icon name="refresh" /> {starting ? 'Starting…' : 'Release all now'}
            </button>
          )}
          {!status.enabled && status.drain_active && (
            <button ref={stopRef} className="button secondary" type="button" onClick={() => openSwitch(false, true)}>
              <Icon name="pause" /> Stop Release all
            </button>
          )}
        </div>
      </header>

      {!status.allowed && (
        <div className="message pattern-blocked" role="note"><Icon name="alert" /><span>Automatic release is blocked for this ADD deployment (setting ADD_UNKNOWN_IDENTITY_AUTO_RELEASE_ALLOWED is false). Nothing is released automatically, whatever the switch says.</span></div>
      )}
      {failure && <OperatorFailureMessage failure={failure} />}
      {query.isError && <p className="unknown-auto-release-note attention-copy" role="status">The last refresh failed; showing figures from {formatAgo(secondsSince(status.server_time, null))}.</p>}
      {status.backpressure && status.active && (
        <p className="unknown-auto-release-note" role="status"><Icon name="pause" /> Paused for Oracle: {punchCount(progress.in_flight)} are already waiting (limit {status.in_flight_limit.toLocaleString()}). ADD adds the next batch when Oracle catches up, so live punches keep priority.</p>
      )}
      {status.resting_until && status.active && !status.backpressure && (
        <p className="unknown-auto-release-note" role="status">Every such punch has been checked. ADD reads the older held ones again from {dateTime(status.resting_until)} (when skipped punches may be retried); new punches are still picked up every few seconds.</p>
      )}
      {status.last_error_code && <p className="unknown-auto-release-note attention-copy" role="status"><Icon name="alert" /> {errorText(status.last_error_code, errorAt)}</p>}

      {share !== null && (
        <div className="unknown-auto-release-bar">
          <div role="progressbar" aria-label="Confirmed in Oracle out of released and remaining punches" aria-valuemin={0} aria-valuemax={100} aria-valuenow={share}>
            <span style={{ width: `${share}%` }} />
          </div>
          <small>{share}% confirmed in Oracle · {done.toLocaleString()} of {(done + (remaining ?? 0)).toLocaleString()}</small>
        </div>
      )}

      <dl className="unknown-auto-release-stats">
        <div>
          <dt>Still to release</dt>
          <dd><strong>{remaining === null ? '…' : remaining.toLocaleString()}</strong>
            <small>{backlog.counted_at ? `counted ${formatAgo(countedAgo)}` : 'counting · the first count arrives within a minute'}</small>
            {elsewhere && <small>{`+ ${elsewhere} (not counted above)`}</small>}</dd>
        </div>
        <div>
          <dt>Waiting for Oracle</dt>
          <dd><strong>{progress.in_flight.toLocaleString()}</strong>
            <small>{progress.ready.toLocaleString()} queued · {progress.waiting_oracle.toLocaleString()} sent, awaiting confirmation</small></dd>
        </div>
        <div>
          <dt>Confirmed in Oracle</dt>
          <dd><strong>{progress.confirmed_total.toLocaleString()}</strong>
            <small>{progress.confirmed_last_hour.toLocaleString()} in the last hour</small></dd>
        </div>
        <div>
          <dt>Skipped (last {progress.skipped_window_hours} h)</dt>
          <dd><strong>{progress.skipped_total.toLocaleString()}</strong>
            <small>stay held; tried again after {progress.skipped_window_hours} h</small></dd>
        </div>
        <div>
          <dt>Speed</dt>
          <dd><strong>{status.rate_per_min === null ? '…' : `${status.rate_per_min.toLocaleString()}/min`}</strong>
            <small>{etaText(status)}</small></dd>
        </div>
      </dl>

      {progress.skipped_by_reason.length > 0 && (
        <details className="unknown-auto-release-skips">
          <summary>Why {punchCount(progress.skipped_total)} were skipped</summary>
          <ul>{progress.skipped_by_reason.map((item) => <li key={item.code}><strong>{item.count.toLocaleString()}</strong> {item.reason}</li>)}</ul>
          <p>Skipped punches stay held. Use “Send to ORDS” on a punch to decide it by hand; ADD tries them again after {progress.skipped_window_hours} hours in case a roster or CNIC was fixed.</p>
        </details>
      )}
      {progress.needs_review > 0 && <p className="unknown-auto-release-note attention-copy">{punchCount(progress.needs_review)} need a person to review in Direct ORDS.</p>}

      <p className="unknown-auto-release-foot">
        {status.last_batch_at
          ? `Last batch ${formatAgo(secondsSince(status.last_batch_at, status.server_time))}: ${punchCount(status.last_batch_size)}. `
          : 'No batch yet. '}
        {status.batches_created.toLocaleString()} batches, {punchCount(status.punches_submitted)} checked so far.
        {' '}Pace: up to {status.batch_size.toLocaleString()} every {status.min_interval_seconds.toLocaleString()} s, never more than {status.in_flight_limit.toLocaleString()} waiting for Oracle. Updates every 10 seconds.
      </p>

      {target !== null && (
        <AutoReleaseSwitchDialog
          target={target}
          stopDrain={stopDrain}
          status={status}
          returnFocusRef={stopDrain ? stopRef : switchRef}
          onClose={() => setTarget(null)}
          onSaved={(value) => {
            storeAutoReleaseStatus(value)
            setTarget(null)
            toast?.notice(stopDrain
              ? 'Release all stopped; automatic release stays OFF. The change was audited.'
              : `Automatic release is now ${target ? 'ON' : 'OFF'}. The change was audited.`)
            onChanged?.()
            void query.refetch()
          }}
        />
      )}
    </section>
  )
}

export function AutoReleaseSwitchDialog({
  target,
  stopDrain = false,
  status,
  onClose,
  onSaved,
  returnFocusRef,
}: {
  target: boolean
  /** Switch stays OFF; only the running Release all now stops (sent as enabled=false). */
  stopDrain?: boolean
  status: AutoReleaseStatus
  onClose: () => void
  onSaved: (status: AutoReleaseStatus | null) => void
  returnFocusRef?: RefObject<HTMLElement | null>
}) {
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<OperatorFailure | null>(null)
  const key = useRef(idempotency('unknown-auto-release-switch'))
  const trimmed = reason.trim()
  const canSubmit = trimmed.length >= REASON_MIN && Boolean(password) && !busy
  const titleId = 'unknown-auto-release-switch-title'

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return
    const secret = password
    setPassword('')
    setBusy(true)
    setFailure(null)
    try {
      const value = await api<unknown>(`${AUTO_RELEASE_PATH}/control`, {
        method: 'POST',
        body: JSON.stringify({ enabled: target, reason: trimmed, password: secret, idempotency_key: key.current }),
      })
      onSaved(normalizeAutoReleaseStatus(value))
    } catch (error) {
      if (error instanceof ApiError && error.code === 'IDEMPOTENCY_CONFLICT') key.current = idempotency('unknown-auto-release-switch')
      setFailure(explainOperatorError(error, 'The switch could not be changed.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      titleId={titleId}
      title={target ? 'Turn automatic release ON' : stopDrain ? 'Stop Release all now' : 'Turn automatic release OFF'}
      description="Needs your administrator password and a written reason. The change is audited."
      onClose={() => { if (!busy) onClose() }}
      className="operator-action-dialog"
      returnFocusRef={returnFocusRef}
    >
      <form className="dialog-body unknown-auto-release-dialog" onSubmit={(event) => void submit(event)}>
        {target ? (
          <div className="info-copy" role="note">
            <Icon name="info" />
            <div>
              <p>ADD releases every “Unknown identity · CNIC on synced user” punch to Oracle by itself: up to {status.batch_size.toLocaleString()} at a time, at most every {status.min_interval_seconds.toLocaleString()} seconds, and it pauses while {status.in_flight_limit.toLocaleString()} are waiting for Oracle.</p>
              <p>Every punch still passes the Direct ORDS checks. Anything that needs a person stays held.</p>
            </div>
          </div>
        ) : (
          <div className="message pattern-waiting" role="note">
            <Icon name="alert" />
            <div>
              <p><strong>ADD stops creating new release batches straight away.</strong> A batch being prepared at this moment is discarded.</p>
              <p>Punches already queued{status.progress.in_flight ? ` (${punchCount(status.progress.in_flight)} now)` : ''} still go to Oracle and are confirmed there. They cannot be unsent.</p>
              {stopDrain
                ? <p>This stops the Release all now that is running. Automatic release stays OFF; the rest of these punches stay held until it is turned ON or Release all is started again.</p>
                : <p>New punches of this kind stay held until this is turned back ON or someone clicks Release all now.</p>}
            </div>
          </div>
        )}
        <label>
          Audited reason
          <textarea
            value={reason}
            maxLength={REASON_MAX}
            required
            placeholder={`At least ${REASON_MIN} characters`}
            aria-describedby={`${titleId}-reason-count`}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <p id={`${titleId}-reason-count`} className="operator-reason-count" aria-live="polite">
          {trimmed.length} / {REASON_MAX} characters{trimmed.length < REASON_MIN ? ` · ${REASON_MIN - trimmed.length} more needed` : ''}
        </p>
        <label>
          Administrator password
          <input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        {failure && <OperatorFailureMessage failure={failure} />}
        <div className="dialog-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={onClose}>Cancel</button>
          <button type="submit" className="button primary" disabled={!canSubmit}>
            <Icon name="shield" /> {busy ? 'Saving…' : target ? 'Turn ON' : stopDrain ? 'Stop Release all' : 'Turn OFF'}
          </button>
        </div>
      </form>
    </Dialog>
  )
}
