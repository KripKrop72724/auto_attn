// Waiting → Sent to machine → Confirmed for one request sent to the machine,
// with Cancel while it waits and the plain reason when it didn't work.
import { canCancelCommand, type CommandTracker } from '../../lib/useCommandTracker'
import { Button, Steps, TechnicalDetails, type StepsState } from '../../ui'
import { TOOLS_WORDS } from '../../words/device'
import { requestFailure } from '../../words/requests'

const WAITING_NOTES: Record<string, string> = {
  WAITING_FOR_DEVICE: TOOLS_WORDS.waitingLinkBox,
  WAITING_FOR_ZKT: TOOLS_WORDS.waitingMachine,
  RETRYING: TOOLS_WORDS.tryingAgain,
}

export function stepsState(tracker: CommandTracker): StepsState {
  const status = String(tracker.command?.status || '').toUpperCase()
  if (tracker.cancelling || status === 'CANCEL_REQUESTED') return 'cancelling'
  if (status === 'EXPIRED') return 'timed-out'
  return tracker.step === 'cancelled' ? 'cancelled' : tracker.step || 'waiting'
}

export function CommandSteps({ tracker, done, onRetry, onCancelled, onCancelFailed }: {
  tracker: CommandTracker
  /** The final line on success ("People list refreshed: 189 people."). */
  done?: string
  onRetry?: () => void
  onCancelled?: () => void
  onCancelFailed?: () => void
}) {
  if (!tracker.command && !tracker.step) return null
  const command = tracker.command
  const status = String(command?.status || '').toUpperCase()
  const state = stepsState(tracker)
  const failure = requestFailure(command?.error_code).sentence
  const cancel = async () => {
    try {
      await tracker.cancel()
      onCancelled?.()
    } catch {
      onCancelFailed?.()
    }
  }
  return (
    <div className="dv-command-steps" aria-live="polite">
      <Steps state={state} failedReason={state === 'failed' ? failure.replace(/\.$/, '') : undefined} />
      {state === 'waiting' && WAITING_NOTES[status] && <p className="dv-muted">{WAITING_NOTES[status]}</p>}
      {(state === 'waiting' || state === 'sent') && <p className="dv-muted">{TOOLS_WORDS.keepTrying}</p>}
      {tracker.checkFailed && <p className="dv-muted">{TOOLS_WORDS.checkFailed}</p>}
      {state === 'confirmed' && done && <p className="dv-result" role="status">{done}</p>}
      {state === 'failed' && <p className="dv-result is-failed">{TOOLS_WORDS.didntWork(failure)}</p>}
      {state === 'cancelled' && <p className="dv-result">{TOOLS_WORDS.cancelled}</p>}
      <div className="dv-tool-actions">
        {canCancelCommand(command) && <Button size="sm" busy={tracker.cancelling} busyLabel="Cancelling…" onClick={() => void cancel()}>{TOOLS_WORDS.cancel}</Button>}
        {(state === 'failed' || state === 'timed-out') && onRetry && <Button size="sm" onClick={onRetry}>{TOOLS_WORDS.tryAgain}</Button>}
      </div>
      {command && (command.error_code || command.error_message) && (
        <TechnicalDetails>
          <dl className="dv-tech-rows">
            <div><dt>Request</dt><dd>{command.type} · {command.command_id}</dd></div>
            <div><dt>Status</dt><dd>{command.status}</dd></div>
            {command.error_code && <div><dt>Code</dt><dd>{command.error_code}</dd></div>}
            {command.error_message && <div><dt>Message</dt><dd>{command.error_message}</dd></div>}
          </dl>
        </TechnicalDetails>
      )}
    </div>
  )
}
