// "Mark this problem as fixed" (PRB-6). The safeguards stay exactly as today
// (owner decision D-2 pending): a reason of at least 10 letters, your password,
// one request key per dialog (a new one only after IDEMPOTENCY_KEY_REUSED) and
// the password cleared after every try. Only the words are plain now.
import { useId, useRef, useState, type FormEvent } from 'react'
import { api, ApiError } from '../../api'
import { idempotency } from '../../lib/idempotency'
import { Button, Callout, Dialog, DivisionTag, TechnicalDetails, useToast } from '../../ui'
import { MARK_FIXED, problemWords } from '../../words/problems'
import type { AlertResolveResponse } from '../../types'
import { deviceLabel } from './ProblemCard'
import type { ProblemDevice, ProblemRow } from './types'
import './problems.css'

type LegacyToast = { notice: (text: string) => void; error?: (text: string) => void }

export interface MarkFixedFailure {
  code: string | null
  summary: string
  /** A plain extra line shown under the summary (for example how it closes). */
  hint?: string
  /** The backend's own sentence and code, for Technical details. */
  technical: string[]
}

/** Plain words for a refused or failed Mark as fixed. */
export function markFixedFailure(error: unknown, closesWhen?: string): MarkFixedFailure {
  if (!(error instanceof ApiError)) {
    return { code: null, summary: MARK_FIXED.failed, technical: error instanceof Error && error.message ? [error.message] : [] }
  }
  const technical = [error.code, error.message, typeof error.detail?.clear_condition === 'string' ? error.detail.clear_condition : null]
    .filter((line): line is string => Boolean(line))
  if (error.status === 403) return { code: error.code, summary: MARK_FIXED.wrongPassword, technical }
  const known = error.code ? MARK_FIXED.refusals[error.code] : undefined
  return {
    code: error.code,
    summary: known || MARK_FIXED.failed,
    hint: error.code === 'ALERT_CONDITION_CURRENT' ? closesWhen : undefined,
    technical,
  }
}

export interface MarkFixedDialogProps {
  /** The problem; `device` is used for the place name when no `deviceName` is given. */
  alert: Pick<ProblemRow, 'id' | 'code' | 'message'> & Partial<Pick<ProblemRow, 'plain_title' | 'plain_detail' | 'next_step'>> & { device?: ProblemDevice }
  /** The place, e.g. "Building 9 · Device 3" (the device page passes it). */
  deviceName?: string | null
  division?: 'life' | 'health' | null
  /** Old toast shape; used when `onView` is not given. */
  toast?: LegacyToast
  onClose: () => void
  onResolved?: (response: AlertResolveResponse) => Promise<void> | void
  /** "Already fixed": the dialog closes and the card moves to Fixed. */
  onGone?: () => void
  /** A refusal: the card's buttons refresh from the next list load. */
  onRefused?: () => void
  /** Adds [View] to the success toast (opens the Fixed tab). */
  onView?: () => void
}

export function MarkFixedDialog({ alert, deviceName, division, toast, onClose, onResolved, onGone, onRefused, onView }: MarkFixedDialogProps) {
  const ids = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const titleId = `mark-fixed-${ids}`
  const words = problemWords(alert)
  const place = alert.device ? deviceLabel(alert.device) : null
  const name = deviceName || place?.name || ''
  const tagDivision = division || place?.division || null
  const uiToast = useToast()
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<MarkFixedFailure | null>(null)
  const key = useRef(idempotency('alert-resolve'))
  const trimmed = reason.trim()
  const missing = Math.max(0, 10 - trimmed.length)
  const canSubmit = trimmed.length >= 10 && Boolean(password) && !busy
  const memory = alert.code === 'ESP_DURABILITY_FAULT'

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return
    const secret = password
    setPassword('')
    setBusy(true)
    setFailure(null)
    try {
      const response = await api<AlertResolveResponse>(`/api/v1/alerts/${alert.id}/resolve`, {
        method: 'POST',
        body: JSON.stringify({ reason: trimmed, password: secret, idempotency_key: key.current }),
      })
      const text = response.residual_alert_id ? `${MARK_FIXED.toast} ${MARK_FIXED.toastReminder}` : MARK_FIXED.toast
      if (onView) uiToast.show({ tone: 'success', text, action: { label: MARK_FIXED.view, onClick: onView }, duration: response.residual_alert_id ? 6000 : 5000 })
      else if (toast) toast.notice(text)
      else uiToast.show({ tone: 'success', text })
      onClose()
      void Promise.resolve(onResolved?.(response)).catch(() => undefined)
    } catch (error) {
      if (error instanceof ApiError && error.code === 'IDEMPOTENCY_KEY_REUSED') key.current = idempotency('alert-resolve')
      if (error instanceof ApiError && error.code === 'ALERT_NOT_ACTIVE') {
        uiToast.show({ tone: 'info', text: MARK_FIXED.refusals.ALERT_NOT_ACTIVE })
        onClose()
        onGone?.()
        return
      }
      if (error instanceof ApiError && (error.code === 'ALERT_CONDITION_CURRENT' || error.code === 'ALERT_WORKFLOW_OWNED')) onRefused?.()
      setFailure(markFixedFailure(error, words.closesWhen))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      titleId={titleId}
      title={MARK_FIXED.title}
      description={name ? `${words.title} · ${name}` : words.title}
      onClose={() => { if (!busy) onClose() }}
      className="operator-action-dialog mark-fixed-dialog"
      dirty={Boolean(trimmed)}
      initialFocus="first-field"
    >
      <form className="dialog-body mark-fixed-form" onSubmit={(event) => void submit(event)}>
        {tagDivision && name && <p className="mark-fixed-place"><DivisionTag division={tagDivision} /><span>{name}</span></p>}
        <p className="mark-fixed-intro">{MARK_FIXED.comesBack}</p>
        <div className="mark-fixed-field">
          <label className="mark-fixed-label" htmlFor={`${titleId}-reason`}>{MARK_FIXED.reason}</label>
          <span className="mark-fixed-help" id={`${titleId}-reason-help`}>{MARK_FIXED.reasonHelp}</span>
          <textarea
            id={`${titleId}-reason`}
            value={reason}
            maxLength={500}
            rows={3}
            placeholder={MARK_FIXED.placeholder}
            aria-describedby={`${titleId}-reason-help ${titleId}-reason-count`}
            onChange={(event) => setReason(event.target.value)}
          />
          <p id={`${titleId}-reason-count`} className="mark-fixed-count" aria-live="polite">{missing > 0 ? MARK_FIXED.more(missing) : ''}</p>
        </div>
        <div className="mark-fixed-field">
          <label className="mark-fixed-label" htmlFor={`${titleId}-password`}>{MARK_FIXED.password}</label>
          <input id={`${titleId}-password`} type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
        </div>
        {memory && <Callout tone="info">{MARK_FIXED.reminder}</Callout>}
        {failure && (
          <div className="message pattern-blocked mark-fixed-error" role="alert">
            <div>
              <strong>{failure.summary}</strong>
              {failure.hint && <p>{failure.hint}</p>}
              {failure.technical.length > 0 && <TechnicalDetails>{failure.technical.map((line) => <p key={line}>{line}</p>)}</TechnicalDetails>}
            </div>
          </div>
        )}
        <div className="dialog-actions">
          <Button variant="secondary" disabled={busy} onClick={onClose}>{MARK_FIXED.cancel}</Button>
          <Button type="submit" variant="primary" icon="check" busy={busy} busyLabel={MARK_FIXED.busy} disabled={!canSubmit && !busy}>{MARK_FIXED.submit}</Button>
        </div>
      </form>
    </Dialog>
  )
}
