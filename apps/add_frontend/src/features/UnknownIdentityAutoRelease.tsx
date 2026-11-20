import { useRef, useState, type FormEvent, type RefObject } from 'react'
import { api, ApiError } from '../api'
import { Dialog, idempotency } from '../App'
import { Icon } from '../Icon'
import { TechnicalDetails } from '../ui'
import { checkins, longTime, rowTime, skipWords } from './attendance/attendanceWords'
import { explainOperatorError, OperatorFailureMessage, type OperatorFailure } from './AlertResolveDialog'
import {
  AUTO_RELEASE_PATH,
  elsewhereText,
  etaText,
  formatAgo,
  normalizeAutoReleaseStatus,
  remainingOf,
  secondsSince,
  storeAutoReleaseStatus,
  useUnknownAutoReleaseStatus,
  type AutoReleaseStatus,
} from './unknownAutoRelease'
import './UnknownIdentityAutoRelease.css'
import { STEP_UP } from '../words/glossary'

type Toast = { notice: (message: string) => void; error: (message: string) => void }

const REASON_MIN = 3
const REASON_MAX = 500

const errorText = (code: string, at: string) => {
  if (code === 'ADD_BUSY') return `The dashboard was busy ${at}; the next group tries again by itself.`
  if (code === 'COUNT_BUSY') return `The count was skipped ${at} because the dashboard was busy; it is tried again in a minute.`
  return `A group of check-ins stopped ${at}; sending moved on and keeps going.`
}

function stateBadge(status: AutoReleaseStatus) {
  if (!status.allowed) return { text: 'Switched off for now · Ask IT to switch it on', pattern: 'blocked' }
  if (status.enabled) return { text: status.backpressure ? 'On · waiting for the HR system' : 'On · sending by itself', pattern: 'confirmed' }
  if (status.drain_active) return { text: 'Off · sending all now', pattern: 'waiting' }
  return { text: 'Off', pattern: 'waiting' }
}

function stateSentence(status: AutoReleaseStatus) {
  const when = status.changed_at ? ` since ${longTime(status.changed_at)}` : ''
  const by = status.changed_by ? `, turned ${status.enabled ? 'on' : 'off'} by ${status.changed_by}` : ''
  if (status.enabled) {
    return status.changed_by
      ? `On${when}${by}. These check-ins are sent to the HR system by themselves, a few at a time. Nothing to do.`
      : 'On (the normal setting). These check-ins are sent to the HR system by themselves, a few at a time. Nothing to do.'
  }
  if (status.drain_active) {
    return `Off${when}${by}. Send all now was started${status.drain_requested_by ? ` by ${status.drain_requested_by}` : ''}${status.drain_requested_at ? ` at ${rowTime(status.drain_requested_at)}` : ''}; it stops by itself once every such check-in has been tried.`
  }
  return `Off${when}${by}. New check-ins like these wait until it is turned on again, or someone presses Send all now.`
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
    return <p className="unknown-auto-release-loading" role="status"><span className="ui-skeleton ui-skeleton-line" aria-hidden="true" /><span className="sr-only">Loading</span></p>
  }
  if (!status) {
    return query.isError
      ? <div className="message pattern-blocked" role="alert"><Icon name="alert" /><span>Couldn't load this. Trying again every 10 seconds.</span></div>
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
        ? 'Automatic sending is on, so these check-ins are already being sent.'
        : outcome === 'ALREADY_RUNNING'
          ? 'Send all now is already running.'
          : 'Sending all now. Progress shows here.')
      onChanged?.()
    } catch (error) {
      setFailure(explainOperatorError(error, "Couldn't start sending all."))
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
          <h3 id="unknown-auto-release-title">Send not-matched check-ins automatically</h3>
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
            <span>Send automatically</span>
            <strong aria-hidden="true">{status.enabled ? 'On' : 'Off'}</strong>
          </button>
          {!status.enabled && status.allowed && !status.drain_active && (
            <button className="button primary" type="button" disabled={starting} onClick={() => void releaseAll()}>
              <Icon name="refresh" /> {starting ? 'Starting…' : 'Send all now'}
            </button>
          )}
          {!status.enabled && status.drain_active && (
            <button ref={stopRef} className="button secondary" type="button" onClick={() => openSwitch(false, true)}>
              <Icon name="pause" /> Stop sending all
            </button>
          )}
        </div>
      </header>

      {!status.allowed && (
        <div className="message pattern-blocked" role="note"><Icon name="alert" /><span>Automatic sending is switched off for now, so nothing is sent by itself whatever the switch says. Ask IT to switch it on.</span></div>
      )}
      {failure && <OperatorFailureMessage failure={failure} />}
      {query.isError && <p className="unknown-auto-release-note attention-copy" role="status">Couldn't update. Showing figures from {formatAgo(secondsSince(status.server_time, null))}.</p>}
      {status.backpressure && status.active && (
        <p className="unknown-auto-release-note" role="status"><Icon name="pause" /> Paused for a moment: {checkins(progress.in_flight)} are already on the way to the HR system. More are sent when it catches up, so today's check-ins go first.</p>
      )}
      {status.resting_until && status.active && !status.backpressure && (
        <p className="unknown-auto-release-note" role="status">Every such check-in has been tried. The older ones are tried again from {rowTime(status.resting_until)}; new ones are still picked up every few seconds.</p>
      )}
      {status.last_error_code && <p className="unknown-auto-release-note attention-copy" role="status"><Icon name="alert" /> {errorText(status.last_error_code, errorAt)}</p>}

      {share !== null && (
        <div className="unknown-auto-release-bar">
          <div role="progressbar" aria-label="Sent so far" aria-valuemin={0} aria-valuemax={100} aria-valuenow={share}>
            <span style={{ width: `${share}%` }} />
          </div>
          <small>{share}% sent · {done.toLocaleString()} of {(done + (remaining ?? 0)).toLocaleString()}</small>
        </div>
      )}

      <dl className="unknown-auto-release-stats">
        <div>
          <dt>Still to send</dt>
          <dd><strong>{remaining === null ? '…' : remaining.toLocaleString()}</strong>
            <small>{backlog.counted_at ? `counted ${formatAgo(countedAgo)}` : 'counting · the first count arrives within a minute'}</small>
            {elsewhere && <small>{`+ ${elsewhere} (not counted above)`}</small>}</dd>
        </div>
        <div>
          <dt>On the way</dt>
          <dd><strong>{progress.in_flight.toLocaleString()}</strong>
            <small>{progress.ready.toLocaleString()} waiting · {progress.waiting_oracle.toLocaleString()} waiting for the HR system to confirm</small></dd>
        </div>
        <div>
          <dt>Sent</dt>
          <dd><strong>{progress.confirmed_total.toLocaleString()}</strong>
            <small>{progress.confirmed_last_hour.toLocaleString()} in the last hour</small></dd>
        </div>
        <div>
          <dt>Not sent (last {progress.skipped_window_hours} hours)</dt>
          <dd><strong>{progress.skipped_total.toLocaleString()}</strong>
            <small>tried again after {progress.skipped_window_hours} hours</small></dd>
        </div>
        <div>
          <dt>Speed</dt>
          <dd><strong>{status.rate_per_min === null ? '…' : `${status.rate_per_min.toLocaleString()}/min`}</strong>
            <small>{etaText(status)}</small></dd>
        </div>
      </dl>

      {progress.skipped_by_reason.length > 0 && (
        <details className="unknown-auto-release-skips">
          <summary>Why {checkins(progress.skipped_total)} were not sent</summary>
          <ul>{progress.skipped_by_reason.map((item) => <li key={item.code}><strong>{item.count.toLocaleString()}</strong> {skipWords(item.code)}</li>)}</ul>
          <p>Open a check-in to send it by hand. These are tried again after {progress.skipped_window_hours} hours, in case a CNIC was fixed.</p>
        </details>
      )}
      {progress.needs_review > 0 && <p className="unknown-auto-release-note attention-copy">{checkins(progress.needs_review)} are waiting for you to choose. Use Send by hand.</p>}

      <p className="unknown-auto-release-foot">
        {status.last_batch_at
          ? `Last sent ${formatAgo(secondsSince(status.last_batch_at, status.server_time))}: ${status.last_batch_size.toLocaleString()}. `
          : 'Nothing sent yet. '}
        Updates every 10 seconds. This setting covers Life and Health.
      </p>
      <TechnicalDetails>
        <dl className="attendance-tech-list">
          <div><dt>Groups so far</dt><dd>{status.batches_created.toLocaleString()} · {status.punches_submitted.toLocaleString()} checked</dd></div>
          <div><dt>Pace</dt><dd>up to {status.batch_size.toLocaleString()} every {status.min_interval_seconds.toLocaleString()} s, at most {status.in_flight_limit.toLocaleString()} on the way</dd></div>
          {status.last_error_code && <div><dt>Last error code</dt><dd>{status.last_error_code}</dd></div>}
          {status.progress.skipped_by_reason.map((item) => <div key={item.code}><dt>{item.code}</dt><dd>{item.reason}</dd></div>)}
        </dl>
      </TechnicalDetails>

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
              ? 'Stopped sending all. Automatic sending stays off.'
              : `Automatic sending is now ${target ? 'on' : 'off'}.`)
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
      setFailure(explainOperatorError(error, "Couldn't change the switch."))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      titleId={titleId}
      title={target ? 'Turn on automatic sending' : stopDrain ? 'Stop sending all' : 'Turn off automatic sending'}
      description="Needs your password and a reason. The change is saved in the records."
      onClose={() => { if (!busy) onClose() }}
      className="operator-action-dialog"
      returnFocusRef={returnFocusRef}
    >
      <form className="dialog-body unknown-auto-release-dialog" onSubmit={(event) => void submit(event)}>
        {target ? (
          <div className="info-copy" role="note">
            <Icon name="info" />
            <div>
              <p>Not-matched check-ins with a CNIC found on the machine are sent to the HR system by themselves, a few at a time, and sending pauses while many are already on the way.</p>
              <p>Every check-in still passes the same checks as Send by hand. Anything that needs a person is not sent.</p>
            </div>
          </div>
        ) : (
          <div className="message pattern-waiting" role="note">
            <Icon name="alert" />
            <div>
              <p><strong>No new check-ins start sending.</strong> A group being prepared right now is dropped.</p>
              <p>Check-ins already on the way{status.progress.in_flight ? ` (${checkins(status.progress.in_flight)} now)` : ''} still reach the HR system. They can't be taken back.</p>
              {stopDrain
                ? <p>This stops Send all now. Automatic sending stays off; the rest of these check-ins wait until it is turned on or Send all now is started again.</p>
                : <p>New check-ins like these wait until this is turned on again, or someone presses Send all now.</p>}
            </div>
          </div>
        )}
        <label>
          {STEP_UP.why}
          <textarea
            value={reason}
            maxLength={REASON_MAX}
            required
            aria-describedby={`${titleId}-reason-help ${titleId}-reason-count`}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <small id={`${titleId}-reason-help`} className="step-up-help">{STEP_UP.whyHelp(REASON_MIN)}</small>
        <p id={`${titleId}-reason-count`} className="operator-reason-count" aria-live="polite">
          {STEP_UP.whyCount(trimmed.length, REASON_MIN)}
        </p>
        <label>
          {STEP_UP.password}
          <input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        <small className="step-up-help">{STEP_UP.passwordHelp}</small>
        {failure && <OperatorFailureMessage failure={failure} />}
        <div className="dialog-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={onClose}>Cancel</button>
          <button type="submit" className="button primary" disabled={!canSubmit}>
            <Icon name="shield" /> {busy ? 'Saving…' : target ? 'Turn on' : stopDrain ? 'Stop sending all' : 'Turn off'}
          </button>
        </div>
      </form>
    </Dialog>
  )
}
