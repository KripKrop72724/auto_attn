import { useRef, useState, type FormEvent, type ReactNode } from 'react'
import { api, ApiError } from '../api'
import { Dialog, idempotency, type useToast } from '../App'
import { Icon } from '../Icon'
import { humanizeStatus } from '../status'
import type { Alert, AlertResolveResponse, DeviceErrorClearResponse, DeviceErrorState } from '../types'
import './DeviceHealth.css'

type Toast = Pick<ReturnType<typeof useToast>, 'notice' | 'error'>

export interface OperatorFailure {
  code: string | null
  summary: string
  details: string[]
}

// Every 409 and 403 arrives as {"detail": {"code", "message", ...extra}}.
const refusals: Record<string, string> = {
  ALERT_NOT_ACTIVE: 'This alert is no longer active: new evidence or another administrator already resolved it.',
  ALERT_CONDITION_CURRENT: 'The device still reports this condition, so it cannot be resolved by hand. ADD resolves it when the condition clears.',
  ALERT_WORKFLOW_OWNED: 'This alert belongs to its own review workflow, which resolves it. Close it from that workflow instead.',
  IDEMPOTENCY_KEY_REUSED: 'This request key was already used for a different action. A new key is ready; submit again.',
  DEVICE_ERROR_CHANGED: 'The stored device error changed after this page loaded. Close this dialog and review the device again.',
  DEVICE_ERROR_STILL_BACKED: 'An active alert still backs this device error, so it stays until that alert resolves.',
}

export function explainOperatorError(error: unknown, fallback: string): OperatorFailure {
  if (!(error instanceof ApiError)) {
    return { code: null, summary: error instanceof Error && error.message ? error.message : fallback, details: [] }
  }
  const known = error.code ? refusals[error.code] : undefined
  const details: string[] = []
  if (known && error.message && error.message !== known) details.push(error.message)
  const extra = error.detail || {}
  if (error.code === 'ALERT_CONDITION_CURRENT' && typeof extra.clear_condition === 'string') {
    details.push(extra.clear_condition)
  }
  if (error.code === 'DEVICE_ERROR_STILL_BACKED' && Array.isArray(extra.alert_ids) && extra.alert_ids.length) {
    const ids = extra.alert_ids.filter((id): id is number => typeof id === 'number')
    details.push(`Backing alert${ids.length === 1 ? '' : 's'}: ${ids.map((id) => `#${id}`).join(', ')}.`)
  }
  if (error.status === 403) details.push('The password field was cleared. Enter it again to retry.')
  return { code: error.code, summary: known || error.message || fallback, details }
}

export function OperatorFailureMessage({ failure }: { failure: OperatorFailure }) {
  return (
    <div className="message pattern-blocked" role="alert">
      <Icon name="alert" />
      <div className="operator-failure">
        <strong>{failure.summary}</strong>
        {failure.details.map((line) => <p key={line}>{line}</p>)}
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
      setFailure(explainOperatorError(error, 'The action could not be completed.'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog titleId={titleId} title={title} description={description} onClose={() => { if (!busy) onClose() }} className="operator-action-dialog">
      <form className="dialog-body" onSubmit={(event) => void submit(event)}>
        {children}
        <label>
          Audited reason
          <textarea
            value={reason}
            maxLength={500}
            placeholder="At least 10 characters"
            aria-describedby={`${titleId}-reason-count`}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <p id={`${titleId}-reason-count`} className="operator-reason-count" aria-live="polite">
          {trimmed.length} / 500 characters{trimmed.length < 10 ? ` · ${10 - trimmed.length} more needed` : ''}
        </p>
        <label>
          Administrator password
          <input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        {failure && <OperatorFailureMessage failure={failure} />}
        <div className="dialog-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={onClose}>Cancel</button>
          <button type="submit" className="button primary" disabled={!canSubmit}><Icon name="shield" /> {busy ? busyLabel : submitLabel}</button>
        </div>
      </form>
    </Dialog>
  )
}

export function AlertResolveDialog({
  alert,
  deviceName,
  toast,
  onClose,
  onResolved,
}: {
  alert: Pick<Alert, 'id' | 'code' | 'message'>
  deviceName?: string | null
  toast: Toast
  onClose: () => void
  onResolved?: (response: AlertResolveResponse) => Promise<void> | void
}) {
  const submit = async (body: StepUpBody) => {
    const response = await api<AlertResolveResponse>(`/api/v1/alerts/${alert.id}/resolve`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    const parts = ['Alert resolved with an audit entry.']
    if (response.device_state) parts.push(`Device is now ${humanizeStatus(response.device_state).toLowerCase()}.`)
    if (response.residual_alert_id) {
      parts.push(`Preservation unverified (alert #${response.residual_alert_id}) stays open until storage is verified.`)
    }
    toast.notice(parts.join(' '))
    onClose()
    void Promise.resolve(onResolved?.(response)).catch(() => undefined)
  }
  return (
    <StepUpReasonDialog
      titleId="alert-resolve-title"
      title="Resolve alert with reason"
      description={`${humanizeStatus(alert.code)}${deviceName ? ` · ${deviceName}` : ''}`}
      keyPrefix="alert-resolve"
      submitLabel="Resolve alert"
      busyLabel="Resolving…"
      onSubmit={submit}
      onClose={onClose}
    >
      <article className="info-copy pattern-waiting">
        <Icon name="shield" />
        <div>
          <h3>{alert.message}</h3>
          <p>Your reason is recorded in the audit log. Resolving never moves the alert’s last-seen time; if the device reports the condition again, ADD opens a new alert.</p>
          {alert.code === 'ESP_DURABILITY_FAULT' && <p>Storage evidence: resolving leaves Preservation unverified open as a gating warning until firmware that reports storage diagnostics verifies storage.</p>}
        </div>
      </article>
    </StepUpReasonDialog>
  )
}

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
    const next = response.device_error?.code
    toast.notice(`Device error re-evaluated with an audit entry. ${next ? `The stored error is now ${humanizeStatus(next)}.` : 'No device error remains.'}`)
    onClose()
    void Promise.resolve(onCleared?.(response)).catch(() => undefined)
  }
  return (
    <StepUpReasonDialog
      titleId="device-error-clear-title"
      title="Re-evaluate device error"
      description={`${deviceName} · stored error ${code}`}
      keyPrefix="device-error-clear"
      submitLabel="Re-evaluate device error"
      busyLabel="Re-evaluating…"
      onSubmit={submit}
      onClose={onClose}
    >
      <article className="info-copy pattern-notice">
        <Icon name="info" />
        <div>
          <h3>{humanizeStatus(code)} is not backed by an active alert</h3>
          <p>ADD replaces the stored error with the value it derives from this device’s active alerts: {deviceError.derived_code ? humanizeStatus(deviceError.derived_code) : 'no device error'}. HIL and factory holds that key on the stored error follow the new value.</p>
        </div>
      </article>
    </StepUpReasonDialog>
  )
}
