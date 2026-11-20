// Advanced tools · for IT (04-device §6 Advanced tools). Every existing
// safeguard is kept exactly: reasons, typed phrases (REPLACE / CHANGE /
// REVEAL), passwords and the clean-up preview. Each tool has its own busy
// state and inline errors.
import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../../api'
import { idempotency } from '../../lib/idempotency'
import { Button } from '../../ui'
import type { Toast } from '../../ui/Toast'
import { ADVANCED_WORDS, TOOLS_WORDS, type DeviceDetail } from '../../words/device'
import { problemWords } from '../../words/problems'
import { DeviceErrorClearDialog } from '../AlertResolveDialog'
import { HealthCleanupDialog } from '../HealthCleanupDialog'
import { ZktCustodyStatus } from '../ZktCustodyStatus'
import { CommKeyTools, Field, TypedPhrase } from './CommKeyTools'
import { friendlyName, isHikvision } from './deviceNaming'
import { SpareTool } from './SpareTool'

function ReplaceMachineTool({ device, onChanged }: { device: DeviceDetail; onChanged: () => Promise<void> }) {
  const bound = device.zkt?.confirmed_serial || device.zkt?.expected_serial || ''
  const observed = device.zkt?.serial || ''
  const [reason, setReason] = useState<string>(ADVANCED_WORDS.replaceDefault)
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)
  if (!bound || !observed || bound === observed) return null
  const phrase = `REPLACE ${device.connector_id} ${bound} ${observed}`
  const words = ADVANCED_WORDS.replaceText(bound, observed)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!password) return setError(TOOLS_WORDS.enterPassword)
    setBusy(true)
    setError('')
    try {
      await api(`/api/v1/devices/${encodeURIComponent(device.connector_id)}/terminal-binding/replace`, {
        method: 'POST',
        body: JSON.stringify({
          current_serial: bound,
          observed_serial: observed,
          reason,
          typed_confirmation: confirmation,
          password,
          idempotency_key: idempotency('terminal-replacement'),
        }),
      })
      setPassword('')
      setConfirmation('')
      setDone(true)
      await onChanged()
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 403) {
        setPassword('')
        setError(TOOLS_WORDS.wrongPassword)
      } else setError(ADVANCED_WORDS.replaceFailed)
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="dv-tool-card tone-problem" aria-labelledby="dv-replace-title">
      <h4 id="dv-replace-title">{ADVANCED_WORDS.replaceTitle}</h4>
      <p>{words.before}<strong data-technical>{words.old}</strong>{words.middle}<strong data-technical>{words.fresh}</strong>{words.after}</p>
      {done ? <p role="status">{ADVANCED_WORDS.replaced}</p> : (
        <form className="dv-form" onSubmit={(event) => void submit(event)} noValidate>
          <Field label={ADVANCED_WORDS.why}><input value={reason} onChange={(event) => setReason(event.target.value)} data-user-text /></Field>
          <TypedPhrase phrase={phrase} value={confirmation} onChange={setConfirmation} />
          <Field label={TOOLS_WORDS.password}><input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} aria-invalid={Boolean(error) || undefined} /></Field>
          {error && <p className="field-error" role="alert">{error}</p>}
          <div className="dv-tool-actions">
            <Button type="submit" variant="danger" busy={busy} busyLabel={ADVANCED_WORDS.confirming} disabled={!reason.trim() || confirmation !== phrase || !password}>{ADVANCED_WORDS.replaceButton}</Button>
          </div>
        </form>
      )}
    </section>
  )
}

export function AdvancedTools({ device, custodyRevision, toast, hasOpenProblems, onChanged, onInventoryChanged }: {
  device: DeviceDetail
  custodyRevision: number
  toast: Toast
  hasOpenProblems: boolean
  onChanged: () => Promise<void>
  onInventoryChanged: () => Promise<void>
}) {
  const [clearOpen, setClearOpen] = useState(false)
  const [cleanupOpen, setCleanupOpen] = useState(false)
  const [custody, setCustody] = useState(false)
  const name = friendlyName(device)
  const hikvision = isHikvision(device)
  const deviceError = device.health?.device_error
  const staleError = Boolean(deviceError?.code && !deviceError.backed)
  const afterAction = async () => { await Promise.all([onChanged(), onInventoryChanged()]) }
  return (
    <div className="dv-tool-list">
      <ReplaceMachineTool device={device} onChanged={onChanged} />
      <SpareTool device={device} onInventoryChanged={onInventoryChanged} />
      {!hikvision && <CommKeyTools device={device} onChanged={onChanged} />}
      {staleError && deviceError?.code && (
        <section className="dv-tool-card" aria-labelledby="dv-clear-error-title">
          <h4 id="dv-clear-error-title">{ADVANCED_WORDS.clearTitle}</h4>
          <p>{ADVANCED_WORDS.clearText(problemWords({ code: deviceError.code }).title)}</p>
          <div className="dv-tool-actions"><Button onClick={() => setClearOpen(true)}>{ADVANCED_WORDS.clearButton}</Button></div>
        </section>
      )}
      {hasOpenProblems && (
        <section className="dv-tool-card" aria-labelledby="dv-cleanup-title">
          <h4 id="dv-cleanup-title">{ADVANCED_WORDS.cleanupTitle}</h4>
          <p>{ADVANCED_WORDS.cleanupText}</p>
          <div className="dv-tool-actions"><Button onClick={() => setCleanupOpen(true)}>{ADVANCED_WORDS.cleanupButton}</Button></div>
        </section>
      )}
      {!hikvision && (
        <section className="dv-tool-card" aria-labelledby="dv-custody-title">
          <h4 id="dv-custody-title">{ADVANCED_WORDS.custodyTitle}</h4>
          {custody
            ? <ZktCustodyStatus connectorId={device.connector_id} revision={custodyRevision} />
            : <div className="dv-tool-actions"><Button onClick={() => setCustody(true)}>{ADVANCED_WORDS.custodyShow}</Button></div>}
        </section>
      )}
      {clearOpen && deviceError?.code && <DeviceErrorClearDialog
        connectorId={device.connector_id}
        deviceName={name}
        deviceError={deviceError}
        toast={toast}
        onClose={() => setClearOpen(false)}
        onCleared={afterAction}
      />}
      {cleanupOpen && <HealthCleanupDialog
        devices={[device]}
        initialChosen={[device.connector_id]}
        toast={toast}
        onClose={() => setCleanupOpen(false)}
        onApplied={afterAction}
      />}
    </div>
  )
}
