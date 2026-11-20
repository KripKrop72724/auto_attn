import { useRef, useState, type FormEvent, type ReactNode } from 'react'
import { api, ApiError } from '../api'
import { Dialog, idempotency, type useToast } from '../App'
import { Icon } from '../Icon'
import { plainApiError } from '../lib/plainApiError'
import { TechnicalDetails } from '../ui'
import { ADVANCED_WORDS } from '../words/device'
import { STEP_UP } from '../words/glossary'
import { MARK_FIXED, problemWords } from '../words/problems'
import type { DeviceErrorClearResponse, DeviceErrorState } from '../types'
import './DeviceHealth.css'

type Toast = Pick<ReturnType<typeof useToast>, 'notice' | 'error'>

export interface OperatorFailure {
  code: string | null
  summary: string
  /** Plain follow-up lines. */
  details: string[]
  /** Backend text, codes and ids: shown only inside Technical details. */
  technical?: string[]
}

// Every 409 and 403 arrives as {"detail": {"code", "message", ...extra}}.
// Plain words only (writing rule 6); the backend message goes to Technical details.
const refusals: Record<string, string> = {
  ...MARK_FIXED.refusals,
  IDEMPOTENCY_KEY_REUSED: 'Something went wrong. Press the button again.',
  DEVICE_ERROR_CHANGED: 'This device changed since you opened this. Close and check again.',
  DEVICE_ERROR_STILL_BACKED: 'An open problem still keeps this error. It clears when that problem is fixed.',
}

export function explainOperatorError(error: unknown, fallback: string): OperatorFailure {
  const plain = plainApiError(error, fallback, refusals)
  const technical = plain.raw ? [plain.raw] : []
  if (!(error instanceof ApiError)) return { code: null, summary: plain.sentence, details: [], technical }
  const details: string[] = []
  const extra = error.detail || {}
  if (error.code === 'ALERT_CONDITION_CURRENT' && typeof extra.clear_condition === 'string') {
    technical.push(extra.clear_condition)
  }
  if (error.code === 'DEVICE_ERROR_STILL_BACKED' && Array.isArray(extra.alert_ids) && extra.alert_ids.length) {
    const ids = extra.alert_ids.filter((id): id is number => typeof id === 'number')
    technical.push(`Problem ${ids.map((id) => `#${id}`).join(', ')}`)
  }
  if (error.status === 403) details.push('Type your password again.')
  return { code: error.code, summary: plain.sentence, details, technical }
}

export function OperatorFailureMessage({ failure }: { failure: OperatorFailure }) {
  return (
    <div className="message pattern-blocked" role="alert">
      <Icon name="alert" />
      <div className="operator-failure">
        <strong>{failure.summary}</strong>
        {failure.details.map((line) => <p key={line}>{line}</p>)}
        {failure.technical && failure.technical.length > 0 && <TechnicalDetails>{failure.technical.map((line) => <p key={line}>{line}</p>)}</TechnicalDetails>}
      </div>
    </div>
  )
}

interface StepUpBody {
  reason: string
  password: string
  idempotency_key: string
}

// Reason, password step-up and one idempotency key per dialog. The password is
// cleared after every attempt; a refused key is replaced before the next try.
function StepUpReasonDialog({
  titleId,
  title,
  description,
  keyPrefix,
  submitLabel,
  busyLabel,
  children,
  onSubmit,
  onClose,
}: {
  titleId: string
  title: string
  description: string
  keyPrefix: string
  submitLabel: string
  busyLabel: string
  children?: ReactNode
  onSubmit: (body: StepUpBody) => Promise<void>
  onClose: () => void
}) {
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<OperatorFailure | null>(null)
  const key = useRef(idempotency(keyPrefix))
  const trimmed = reason.trim()
  const canSubmit = trimmed.length >= 10 && Boolean(password) && !busy
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return
    const secret = password
    setPassword('')
    setBusy(true)
    setFailure(null)
    try {
      await onSubmit({ reason: trimmed, password: secret, idempotency_key: key.current })
    } catch (error) {
      if (error instanceof ApiError && error.code === 'IDEMPOTENCY_KEY_REUSED') key.current = idempotency(keyPrefix)
      setFailure(explainOperatorError(error, "That didn't work. Try again."))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog titleId={titleId} title={title} description={description} onClose={() => { if (!busy) onClose() }} className="operator-action-dialog">
      <form className="dialog-body" onSubmit={(event) => void submit(event)}>
        {children}
        <label>
          {STEP_UP.why}
          <textarea
            value={reason}
            maxLength={500}
            aria-describedby={`${titleId}-reason-help ${titleId}-reason-count`}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <small id={`${titleId}-reason-help`} className="step-up-help">{STEP_UP.whyHelp(10)}</small>
        <p id={`${titleId}-reason-count`} className="operator-reason-count" aria-live="polite">
          {STEP_UP.whyCount(trimmed.length, 10)}
        </p>
        <label>
          {STEP_UP.password}
          <input type="password" autoComplete="current-password" aria-describedby={`${titleId}-password-help`} value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        <small id={`${titleId}-password-help`} className="step-up-help">{STEP_UP.passwordHelp}</small>
        {failure && <OperatorFailureMessage failure={failure} />}
        <div className="dialog-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={onClose}>Cancel</button>
          <button type="submit" className="button primary" disabled={!canSubmit}><Icon name="shield" /> {busy ? busyLabel : submitLabel}</button>
        </div>
      </form>
    </Dialog>
  )
}

// "Mark this problem as fixed" lives in problems/MarkFixedDialog.tsx (PRB-6);
// this name stays for the device page until it switches (04-device DEV-7).
export { MarkFixedDialog, MarkFixedDialog as AlertResolveDialog, markFixedFailure } from './problems/MarkFixedDialog'

export function DeviceErrorClearDialog({
  connectorId,
  deviceName,
  deviceError,
  toast,
  onClose,
  onCleared,
}: {
  connectorId: string
  deviceName: string
  deviceError: Pick<DeviceErrorState, 'code' | 'derived_code'>
  toast: Toast
  onClose: () => void
  onCleared?: (response: DeviceErrorClearResponse) => Promise<void> | void
}) {
  const code = deviceError.code || ''
  const submit = async (body: StepUpBody) => {
    const response = await api<DeviceErrorClearResponse>(`/api/v1/devices/${connectorId}/clear-error`, {
      method: 'POST',
      body: JSON.stringify({ expected_code: code, ...body }),
    })
    toast.notice(`Cleared. ${deviceName} now shows its current status.`)
    onClose()
    void Promise.resolve(onCleared?.(response)).catch(() => undefined)
  }
  return (
    <StepUpReasonDialog
      titleId="device-error-clear-title"
      title={ADVANCED_WORDS.clearTitle}
      description={deviceName}
      keyPrefix="device-error-clear"
      submitLabel={ADVANCED_WORDS.clearButton}
      busyLabel="Clearing…"
      onSubmit={submit}
      onClose={onClose}
    >
      <article className="info-copy pattern-notice">
        <Icon name="info" />
        <div>
          <h3>{problemWords({ code }).title} has no open problem behind it</h3>
          <p>The device goes back to its current status. Nothing else changes.</p>
          <TechnicalDetails>
            <p><span data-technical>{code}</span>{deviceError.derived_code ? <> → <span data-technical>{deviceError.derived_code}</span></> : null}</p>
          </TechnicalDetails>
        </div>
      </article>
    </StepUpReasonDialog>
  )
}
