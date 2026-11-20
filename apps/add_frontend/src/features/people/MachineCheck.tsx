// Machine check (07-people P13): "Is this the right machine?" card, the
// confirmation dialog (password kept) and the "almost done" steps.
import { useState } from 'react'
import { api } from '../../api'
import type { Command, Device } from '../../types'
import { Dialog, StatusChip, Steps, TechnicalDetails } from '../../ui'
import { Icon } from '../../Icon'
import { idempotency } from '../../lib/idempotency'
import { MACHINE_CHECK } from '../../words/people'
import { DialogError, peopleErrorText } from './errors'
import { machineBinding, type PeopleDevice } from './model'

export const MACHINE_CHECK_SLOW_MS = 10 * 60_000

export function MachineCheckCard({ device, place, onConfirm }: { device: PeopleDevice; place: string; onConfirm: () => void }) {
  const serial = device.zkt?.serial || ''
  return (
    <section className="people-machine-check" aria-labelledby="people-machine-check-title">
      <span className="people-notice-icon" aria-hidden="true"><Icon name="shield" /></span>
      <div className="people-machine-check-copy">
        <p className="eyebrow">{MACHINE_CHECK.eyebrow}</p>
        <h2 id="people-machine-check-title">{MACHINE_CHECK.title}</h2>
        <p>{MACHINE_CHECK.body(serial, place)}</p>
        <p className="people-muted"><Icon name={device.connected ? 'check' : 'alert'} size="sm" /> {device.connected ? MACHINE_CHECK.ready : MACHINE_CHECK.offline}</p>
      </div>
      <div className="people-machine-check-action">
        <StatusChip tone="attention" label={MACHINE_CHECK.chip} labelPrefix={null} size="sm" />
        <button className="button primary" type="button" onClick={onConfirm}><Icon name="shield" /> {MACHINE_CHECK.cardButton}</button>
        <small>{MACHINE_CHECK.blocked}</small>
      </div>
    </section>
  )
}

export function MachineCheckPending({ device, command, onRetry }: { device: PeopleDevice; command: Command | null; onRetry: () => void }) {
  const serial = device.zkt?.serial || ''
  const status = String(command?.status || '').toUpperCase()
  const startedAt = command?.created_at ? Date.parse(command.created_at) : NaN
  const slow = status === 'EXPIRED' || status === 'FAILED' || (!Number.isNaN(startedAt) && Date.now() - startedAt > MACHINE_CHECK_SLOW_MS)
  const step = status === 'SUCCEEDED' ? 'confirmed' : ['DISPATCHED', 'ACKNOWLEDGED', 'RUNNING'].includes(status) ? 'sent' : 'waiting'
  return (
    <section className="people-machine-check is-pending" aria-label={MACHINE_CHECK.pendingLabel}>
      <span className="people-notice-icon" aria-hidden="true"><Icon name="refresh" /></span>
      <div className="people-machine-check-copy">
        <p className="eyebrow">{MACHINE_CHECK.pendingEyebrow}</p>
        <h2>{MACHINE_CHECK.pendingTitle}</h2>
        <p>{MACHINE_CHECK.pendingBody(serial)}</p>
        <Steps state={step} labels={MACHINE_CHECK.pendingSteps} />
        <p className="people-muted">{device.connected ? MACHINE_CHECK.pendingUsual : MACHINE_CHECK.pendingOffline}</p>
        {slow && <p>{MACHINE_CHECK.slow} <button type="button" className="button secondary small" onClick={onRetry}>{MACHINE_CHECK.tryAgain}</button></p>}
      </div>
      {!device.connected && <div className="people-machine-check-action"><StatusChip tone="offline" label={MACHINE_CHECK.pendingOfflineChip} labelPrefix={null} size="sm" /></div>}
    </section>
  )
}

export function MachineCheckDialog({ device, place, onClose, onAccepted, toast }: {
  device: PeopleDevice
  place: string
  onClose: () => void
  onAccepted: (device: Device, command: Command) => void
  toast: { notice: (text: string) => void }
}) {
  const serial = device.zkt?.serial || ''
  const { migratedWithoutPin } = machineBinding(device)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ sentence: string; raw: string } | null>(null)
  const confirm = async () => {
    if (!serial || !password || busy) return
    setBusy(true)
    setError(null)
    try {
      const response = await api<{ device: Device; command: Command }>(`/api/v1/devices/${device.connector_id}/terminal-binding/confirm`, {
        method: 'POST',
        body: JSON.stringify({ observed_serial: serial, password, idempotency_key: idempotency('terminal-serial-confirmation') }),
      })
      onAccepted(response.device, response.command)
      toast.notice(device.connected ? MACHINE_CHECK.sent : MACHINE_CHECK.queued)
      onClose()
    } catch (reason) {
      setError(peopleErrorText(reason, MACHINE_CHECK.failed))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog titleId="confirm-machine-title" title={migratedWithoutPin ? MACHINE_CHECK.dialogTitleAgain : MACHINE_CHECK.dialogTitle} description={MACHINE_CHECK.dialogDescription} onClose={onClose} dirty={Boolean(password)}>
      <form className="dialog-body people-machine-dialog" onSubmit={(event) => { event.preventDefault(); void confirm() }}>
        <div className="people-machine-evidence">
          <div>
            <p className="eyebrow">{MACHINE_CHECK.dialogMachine}</p>
            <h3>{place}</h3>
          </div>
          <StatusChip tone={device.connected ? 'working' : 'attention'} label={device.connected ? MACHINE_CHECK.onlineChip : MACHINE_CHECK.offlineChip} labelPrefix={null} size="sm" />
        </div>
        <div className="people-serial-readout">
          <small>{MACHINE_CHECK.serialLabel}</small>
          <code data-technical>{serial}</code>
        </div>
        <p className="people-machine-copy"><Icon name="alert" size="sm" /> {MACHINE_CHECK.dialogBody(place)}</p>
        {!device.connected && <div className="message pattern-waiting"><Icon name="refresh" /><span>{MACHINE_CHECK.dialogOffline}</span></div>}
        <TechnicalDetails>{[device.display_name, device.zkt?.model, device.zkt?.ip_address].filter(Boolean).join(' · ')}</TechnicalDetails>
        <label>{MACHINE_CHECK.password}<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label>
        <DialogError error={error} />
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>{MACHINE_CHECK.cancel}</button>
          <button className="button primary" type="submit" disabled={!password || busy}>{busy ? MACHINE_CHECK.confirming : MACHINE_CHECK.confirm}</button>
        </footer>
      </form>
    </Dialog>
  )
}
