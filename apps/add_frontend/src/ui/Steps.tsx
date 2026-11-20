import { Icon } from '../Icon'
import { UI_TEXT } from './uiText'
import { cx } from './cx'
import './steps.css'

export type StepsState = 'waiting' | 'sent' | 'confirmed' | 'failed' | 'cancelling' | 'cancelled' | 'timed-out'

// Backend command statuses mapped to the three steps a person sees. Logic
// only; the words are the kit's fixed step names (or the caller's).
export function commandStepState(status: unknown): StepsState {
  const value = typeof status === 'string' ? status.trim().toUpperCase() : ''
  if (value === 'SUCCEEDED' || value === 'CONFIRMED' || value === 'COMPLETED') return 'confirmed'
  if (value === 'FAILED' || value === 'ERROR') return 'failed'
  if (value === 'CANCEL_REQUESTED') return 'cancelling'
  if (value === 'CANCELLED') return 'cancelled'
  if (value === 'EXPIRED' || value === 'TIMED_OUT') return 'timed-out'
  if (['DISPATCHED', 'SENT', 'RUNNING', 'ACKNOWLEDGED', 'IN_PROGRESS'].includes(value)) return 'sent'
  return 'waiting'
}

const order = ['waiting', 'sent', 'confirmed'] as const

// Waiting → Sent to machine → Confirmed, or the third step says what went
// wrong. The current step carries aria-current="step".
export function Steps({
  state,
  failedReason,
  labels,
  className,
}: {
  state: StepsState
  /** Plain reason after "Failed:", from fe/words. */
  failedReason?: string
  labels?: Partial<Record<'waiting' | 'sent' | 'confirmed', string>>
  className?: string
}) {
  const names = { waiting: UI_TEXT.stepWaiting, sent: UI_TEXT.stepSent, confirmed: UI_TEXT.stepConfirmed, ...labels }
  const ended = state === 'failed' || state === 'cancelled' || state === 'timed-out'
  const currentIndex = state === 'confirmed' || ended ? 2 : state === 'sent' || state === 'cancelling' ? 1 : 0
  const endLabel = state === 'failed'
    ? UI_TEXT.stepFailed(failedReason)
    : state === 'cancelled' ? UI_TEXT.stepCancelled
      : state === 'timed-out' ? UI_TEXT.stepTimedOut
        : names.confirmed
  return (
    <div className={cx('ui-steps-wrap', className)}>
      <ol className={cx('ui-steps', `is-${state}`)} aria-label={UI_TEXT.progress} data-state={state}>
        {order.map((step, index) => {
          const isCurrent = index === currentIndex
          const status = index < currentIndex || (isCurrent && state === 'confirmed') ? 'done' : isCurrent ? (ended ? 'ended' : 'current') : 'todo'
          const label = index === 2 ? endLabel : names[step]
          return (
            <li key={step} className={cx('ui-step', `is-${status}`, isCurrent && ended && state === 'failed' && 'is-failed')} aria-current={isCurrent ? 'step' : undefined}>
              <span className="ui-steps-marker" aria-hidden="true">
                {status === 'done' ? <Icon name="check" size="sm" /> : status === 'ended' ? <Icon name={state === 'failed' ? 'alert' : 'x'} size="sm" /> : <span />}
              </span>
              <span className="ui-step-label">{label}</span>
            </li>
          )
        })}
      </ol>
      {state === 'cancelling' && <p className="ui-steps-note" role="status">{UI_TEXT.stepCancelling}</p>}
    </div>
  )
}
