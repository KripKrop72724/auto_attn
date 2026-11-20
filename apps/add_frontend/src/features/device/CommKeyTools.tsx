// Machine password (COMM Key) and Show the machine password (04-device
// Advanced tools 3-4). Moved from the old drawer with every safeguard
// unchanged: reason, typed CHANGE / REVEAL phrase and your password; the shown
// password hides after 15 s, when the window loses focus, when the tab is
// hidden, or when the device closes (handlers installed before paint).
import { useEffect, useLayoutEffect, useState, type FormEvent, type ReactNode } from 'react'
import { api, ApiError } from '../../api'
import { idempotency } from '../../lib/idempotency'
import { ago, clock } from '../../lib/time'
import type { CommKeyReveal, CommKeyState } from '../../types'
import { Button, ErrorState, Skeleton, TechnicalDetails, useToast } from '../../ui'
import { ADVANCED_WORDS, TOOLS_WORDS, type DeviceDetail } from '../../words/device'
import { commKeyBlock, commKeyError } from '../../words/requests'
import { plainStatus } from '../../words/states'
import { useCommKeyState } from './useDeviceData'

export function TypedPhrase({ phrase, value, onChange }: { phrase: string; value: string; onChange: (value: string) => void }) {
  return (
    <label>
      <span>{ADVANCED_WORDS.typeToConfirm} <code data-technical>{phrase}</code> {ADVANCED_WORDS.toConfirm}</span>
      <input value={value} onChange={(event) => onChange(event.target.value)} autoComplete="off" spellCheck={false} data-user-text />
    </label>
  )
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label><span>{label}</span>{children}</label>
}

const passwordError = (error: unknown, fallback: string) =>
  error instanceof ApiError && error.status === 403 ? TOOLS_WORDS.wrongPassword : fallback

function statusLine(state: CommKeyState): string {
  const management = String(state.management_state || '').toUpperCase()
  if (management === 'APPLIED') return ADVANCED_WORDS.passwordInUse(state.last_verified_at ? ago(state.last_verified_at) : null)
  if (management === 'UNKNOWN' || !state.managed) return ADVANCED_WORDS.notManaged
  if (management === 'PENDING_CAPABILITY') return ADVANCED_WORDS.needsSoftware
  return plainStatus('commKey', management).label
}

function RevealPassword({ device }: { device: DeviceDetail }) {
  const [reason, setReason] = useState<string>(ADVANCED_WORDS.revealDefault)
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [revealed, setRevealed] = useState<CommKeyReveal | null>(null)
  const [left, setLeft] = useState(15)
  const phrase = `REVEAL ${device.connector_id}`
  // Install hiding handlers before the revealed value can be painted.
  useLayoutEffect(() => {
    if (!revealed) return
    const hide = () => setRevealed(null)
    const hideWhenBackgrounded = () => { if (document.visibilityState !== 'visible') hide() }
    const timer = window.setTimeout(hide, 15_000)
    window.addEventListener('blur', hide)
    document.addEventListener('visibilitychange', hideWhenBackgrounded)
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('blur', hide)
      document.removeEventListener('visibilitychange', hideWhenBackgrounded)
    }
  }, [revealed])
  useEffect(() => {
    if (!revealed) return
    setLeft(15)
    const started = Date.now()
    const tick = window.setInterval(() => setLeft(Math.max(0, 15 - Math.floor((Date.now() - started) / 1000))), 1000)
    return () => window.clearInterval(tick)
  }, [revealed])
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!password) return setError(TOOLS_WORDS.enterPassword)
    setBusy(true)
    setError('')
    try {
      const answer = await api<CommKeyReveal>(`/api/v1/devices/${encodeURIComponent(device.connector_id)}/comm-key/reveal`, {
        method: 'POST',
        body: JSON.stringify({ password, reason, typed_confirmation: confirmation }),
      })
      setRevealed(answer)
      setPassword('')
      setConfirmation('')
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 403) setPassword('')
      setError(passwordError(failure, ADVANCED_WORDS.revealFailed))
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="dv-tool-card" aria-labelledby="dv-reveal-title">
      <h4 id="dv-reveal-title">{ADVANCED_WORDS.revealTitle}</h4>
      <p>{ADVANCED_WORDS.revealText}</p>
      {revealed ? (
        <output className="dv-revealed" aria-live="assertive">
          {ADVANCED_WORDS.revealed} <strong data-technical>{revealed.comm_key}</strong> <span className="dv-muted">{ADVANCED_WORDS.hidesIn(left)}</span>
        </output>
      ) : (
        <form className="dv-form" onSubmit={(event) => void submit(event)} noValidate>
          <Field label={ADVANCED_WORDS.revealWhy}><input value={reason} onChange={(event) => setReason(event.target.value)} data-user-text /></Field>
          <TypedPhrase phrase={phrase} value={confirmation} onChange={setConfirmation} />
          <Field label={TOOLS_WORDS.password}><input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} aria-invalid={Boolean(error) || undefined} /></Field>
          {error && <p className="field-error" role="alert">{error}</p>}
          <div className="dv-tool-actions">
            <Button type="submit" busy={busy} busyLabel={TOOLS_WORDS.sending} disabled={!reason.trim() || confirmation !== phrase || !password}>{ADVANCED_WORDS.revealButton}</Button>
          </div>
        </form>
      )}
    </section>
  )
}

export function CommKeyTools({ device, onChanged }: { device: DeviceDetail; onChanged: () => Promise<void> }) {
  const toast = useToast()
  const query = useCommKeyState(device.connector_id, true)
  const state = query.data ?? null
  const [mode, setMode] = useState<'ESP_ONLY' | 'ESP_AND_TERMINAL'>('ESP_ONLY')
  const [newKey, setNewKey] = useState('')
  const [serial, setSerial] = useState(device.zkt?.confirmed_serial || device.zkt?.expected_serial || device.zkt?.serial || '')
  const [reason, setReason] = useState<string>(ADVANCED_WORDS.passwordReasonDefault)
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [cancelBusy, setCancelBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    setSerial((current) => current || device.zkt?.confirmed_serial || device.zkt?.expected_serial || device.zkt?.serial || '')
  }, [device.zkt?.confirmed_serial, device.zkt?.expected_serial, device.zkt?.serial])

  if (query.isPending) return <section className="dv-tool-card"><h4>{ADVANCED_WORDS.passwordTitle}</h4><Skeleton count={2} label={ADVANCED_WORDS.passwordTitle} /></section>
  if (query.isError) return <section className="dv-tool-card"><h4>{ADVANCED_WORDS.passwordTitle}</h4><ErrorState compact onRetry={() => query.refetch()} /></section>

  const phrase = `CHANGE ${device.connector_id} ${serial || '<serial>'}`
  const keyValid = /^[1-9][0-9]{0,9}$/.test(newKey) && Number(newKey) <= 4_294_967_295
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!state) return
    if (!keyValid) return setError(ADVANCED_WORDS.numbersOnly)
    if (!password) return setError(TOOLS_WORDS.enterPassword)
    setBusy(true)
    setError('')
    try {
      await api(`/api/v1/devices/${encodeURIComponent(device.connector_id)}/comm-key/changes`, {
        method: 'POST',
        body: JSON.stringify({
          new_key: newKey,
          mode,
          expected_revision: state.applied_revision,
          expected_terminal_serial: serial,
          reason,
          typed_confirmation: confirmation,
          password,
          idempotency_key: idempotency('comm-key'),
        }),
      })
      toast.show({ tone: 'success', text: state.capabilities?.esp_only ? ADVANCED_WORDS.sentToLinkBox : ADVANCED_WORDS.afterUpdate })
      setNewKey('')
      setPassword('')
      setConfirmation('')
      await Promise.all([query.refetch(), onChanged()])
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 403) setPassword('')
      setError(passwordError(failure, ADVANCED_WORDS.changeFailed))
    } finally {
      setBusy(false)
    }
  }
  const cancel = async () => {
    if (!state?.active_operation) return
    setCancelBusy(true)
    try {
      await api(`/api/v1/comm-key-operations/${encodeURIComponent(state.active_operation.operation_id)}/cancel`, { method: 'POST', body: '{}' })
      toast.show({ tone: 'success', text: ADVANCED_WORDS.changeCancelled })
      await query.refetch()
    } catch {
      toast.show({ tone: 'error', text: ADVANCED_WORDS.cancelFailed })
    } finally {
      setCancelBusy(false)
    }
  }
  const operation = state?.active_operation
  const capabilities = state?.capabilities
  return (
    <>
      <section className="dv-tool-card" aria-labelledby="dv-password-title">
        <h4 id="dv-password-title">{ADVANCED_WORDS.passwordTitle}</h4>
        {!state ? <p>{ADVANCED_WORDS.notManaged}</p> : <>
          <p>{statusLine(state)}</p>
          {!state.enabled && <p className="dv-muted">{ADVANCED_WORDS.switchedOff}</p>}
          {capabilities?.recovery_staging && <p className="dv-muted">{ADVANCED_WORDS.applyAfterUpdate}</p>}
          {state.last_error_code && <p className="field-error">{ADVANCED_WORDS.lastFailed(commKeyError(state.last_error_code).sentence)}</p>}
          {operation ? (
            <div className="dv-tool-actions">
              <p>{ADVANCED_WORDS.inProgress(plainStatus('commKeyMode', operation.mode).label.toLowerCase(), clock(operation.expires_at))}</p>
              <Button size="sm" busy={cancelBusy} busyLabel="Cancelling…" disabled={['RUNNING', 'ACKNOWLEDGED'].includes(operation.status)} onClick={() => void cancel()}>{ADVANCED_WORDS.cancelChange}</Button>
            </div>
          ) : (
            <form className="dv-form" onSubmit={(event) => void submit(event)} noValidate>
              <Field label={ADVANCED_WORDS.changeOn}>
                <select value={mode} onChange={(event) => setMode(event.target.value as 'ESP_ONLY' | 'ESP_AND_TERMINAL')}>
                  <option value="ESP_ONLY">{plainStatus('commKeyMode', 'ESP_ONLY').label}</option>
                  <option value="ESP_AND_TERMINAL" disabled={!capabilities?.esp_and_terminal}>{plainStatus('commKeyMode', 'ESP_AND_TERMINAL').label}</option>
                </select>
              </Field>
              {!capabilities?.esp_and_terminal && <p className="dv-muted">{ADVANCED_WORDS.cantOnMachine(commKeyBlock(capabilities?.esp_and_terminal_block_reason).replace(/\.$/, '').replace(/^./, (letter) => letter.toLowerCase()))}.</p>}
              <Field label={ADVANCED_WORDS.newPassword}>
                <input type="password" inputMode="numeric" autoComplete="new-password" value={newKey} onChange={(event) => setNewKey(event.target.value)} aria-invalid={Boolean(newKey) && !keyValid || undefined} />
              </Field>
              {newKey && !keyValid && <p className="field-error">{ADVANCED_WORDS.numbersOnly}</p>}
              <Field label={ADVANCED_WORDS.machineSerial}><input value={serial} onChange={(event) => setSerial(event.target.value.trim())} data-user-text /></Field>
              {!device.zkt?.serial && <p className="dv-muted">{ADVANCED_WORDS.serialNote}</p>}
              <Field label={ADVANCED_WORDS.why}><input value={reason} onChange={(event) => setReason(event.target.value)} data-user-text /></Field>
              <TypedPhrase phrase={phrase} value={confirmation} onChange={setConfirmation} />
              <Field label={TOOLS_WORDS.password}><input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></Field>
              {error && <p className="field-error" role="alert">{error}</p>}
              <div className="dv-tool-actions">
                <Button type="submit" variant="danger" busy={busy} busyLabel={TOOLS_WORDS.sending} disabled={!state.enabled}>{ADVANCED_WORDS.changeButton}</Button>
              </div>
            </form>
          )}
          <TechnicalDetails>
            <dl className="dv-tech-rows">
              <div><dt>State</dt><dd>{state.management_state}</dd></div>
              <div><dt>Applied revision</dt><dd>{state.applied_revision}</dd></div>
              <div><dt>Desired revision</dt><dd>{state.desired_revision}</dd></div>
              {state.verified_terminal_serial && <div><dt>Confirmed machine serial</dt><dd>{state.verified_terminal_serial}</dd></div>}
              {state.last_error_code && <div><dt>Last error</dt><dd>{state.last_error_code}{state.last_error_message ? ` · ${state.last_error_message}` : ''}</dd></div>}
              {operation && <div><dt>Change</dt><dd>{operation.operation_id} · {operation.mode} · {operation.status} · revision {operation.requested_revision}</dd></div>}
            </dl>
          </TechnicalDetails>
        </>}
      </section>
      {state?.managed && state.reveal_enabled && <RevealPassword device={device} />}
    </>
  )
}
