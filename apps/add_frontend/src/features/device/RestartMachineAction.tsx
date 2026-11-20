// Restart the machine (04-device Tools 3). Shown only for machines the
// backend can restart; blocked while someone has temporary admin access. The
// confirm sheet keeps today's reason box and password (D-5 waits for the owner).
import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../../api'
import { idempotency } from '../../lib/idempotency'
import { clock } from '../../lib/time'
import { useCommandTracker } from '../../lib/useCommandTracker'
import type { Command } from '../../types'
import { Button, Sheet, useToast } from '../../ui'
import { TOOLS_WORDS, restartError, type DeviceDetail } from '../../words/device'
import { CommandSteps } from './CommandSteps'
import { friendlyName, isHikvision } from './deviceNaming'

const ACTIVE_LEASE = ['GRANTING', 'ACTIVE', 'REVOKING', 'OVERDUE']

export function canRestart(device: DeviceDetail) {
  return !isHikvision(device) && device.zkt?.capabilities?.protocol_restart === true
}

export function RestartMachineAction({ device, revision }: { device: DeviceDetail; revision: number }) {
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState<string>(TOOLS_WORDS.reasonDefault)
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [command, setCommand] = useState<Command | null>(null)
  const tracker = useCommandTracker(command, revision)
  const name = friendlyName(device)

  if (!canRestart(device)) {
    return <div className="dv-action"><p>{TOOLS_WORDS.restartNotCapable}</p></div>
  }
  const lease = device.active_lease && ACTIVE_LEASE.includes(String(device.active_lease.state).toUpperCase()) ? device.active_lease : null

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!password) {
      setError(TOOLS_WORDS.enterPassword)
      return
    }
    if (reason.trim().length < 3) {
      setError(TOOLS_WORDS.reasonShort)
      return
    }
    setBusy(true)
    setError('')
    try {
      const answer = await api<Command>(`/api/v1/devices/${encodeURIComponent(device.connector_id)}/restart`, {
        method: 'POST',
        body: JSON.stringify({ reason: reason.trim(), password, idempotency_key: idempotency('restart') }),
      })
      setPassword('')
      setOpen(false)
      setCommand(answer && answer.command_id ? answer : null)
    } catch (failure) {
      const status = failure instanceof ApiError ? failure.status : null
      if (status === 403) setPassword('')
      setError(restartError(status, failure instanceof Error ? failure.message : null))
    } finally {
      setBusy(false)
    }
  }

  const finishedAt = tracker.command?.completed_at
  return (
    <div className="dv-action">
      <p>{TOOLS_WORDS.restartText}</p>
      {lease && <p className="dv-muted">{TOOLS_WORDS.restartLease(lease.expires_at ? clock(lease.expires_at) : null)}</p>}
      <div className="dv-tool-actions">
        <Button variant="danger-outline" disabled={Boolean(lease) || Boolean(command && !tracker.final)} onClick={() => { setError(''); setOpen(true) }}>
          {TOOLS_WORDS.restartOpen}
        </Button>
      </div>
      {command && <CommandSteps
        tracker={tracker}
        done={TOOLS_WORDS.restarted(clock(finishedAt || new Date().toISOString()))}
        onRetry={() => { setCommand(null); setOpen(true) }}
        onCancelled={() => toast.show({ tone: 'success', text: TOOLS_WORDS.cancelled })}
        onCancelFailed={() => toast.show({ tone: 'error', text: TOOLS_WORDS.cancelFailed })}
      />}
      {open && (
        <Sheet
          title={TOOLS_WORDS.restartConfirm(name)}
          description={TOOLS_WORDS.restartText}
          onClose={() => { setOpen(false); setPassword('') }}
          dirty={Boolean(password)}
          initialFocus="first-field"
          footer={(
            <div className="dv-sheet-actions">
              <Button variant="danger" type="submit" form="dv-restart-form" busy={busy} busyLabel={TOOLS_WORDS.sending}>{TOOLS_WORDS.restartButton}</Button>
              <Button variant="secondary" onClick={() => { setOpen(false); setPassword('') }}>{TOOLS_WORDS.cancel}</Button>
            </div>
          )}
        >
          <form id="dv-restart-form" className="dv-form" onSubmit={(event) => void submit(event)} noValidate>
            <label>
              {TOOLS_WORDS.reason}
              <input value={reason} onChange={(event) => setReason(event.target.value)} data-user-text aria-invalid={error === TOOLS_WORDS.reasonShort || undefined} />
            </label>
            <label>
              {TOOLS_WORDS.password}
              <input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} aria-invalid={Boolean(error && error !== TOOLS_WORDS.reasonShort) || undefined} aria-describedby={error ? 'dv-restart-error' : undefined} />
            </label>
            {error && <p className="field-error" id="dv-restart-error" role="alert">{error}</p>}
          </form>
        </Sheet>
      )}
    </div>
  )
}
