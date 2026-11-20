// People dialogs (07-people P6, P7, P8, P11, P12): add, edit, remove one,
// temporary admin access, remove several, "same person" and HR details.
// Every safeguard is kept exactly: the password on every write, the typed
// name or Employee ID to remove one person, the reason and the server's typed
// phrase to remove several, the typed "SAME EMPLOYEE" / "REVOKE RESOLUTION"
// and the HR-details phrase (D-19 waits for the owner, PR-10).
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { api, ApiError } from '../api'
import { Dialog, DivisionTag, TechnicalDetails } from '../ui'
import { Icon } from '../Icon'
import { idempotency } from '../lib/idempotency'
import { buildMachinePreview, bulkDeletionConfirmation, confirmationMatches, utf8Length } from '../lib/people'
import { day } from '../lib/time'
import type {
  HistoricalIdentityDialogState, IdentityResolutionDialogState, UserDialogState,
} from '../lib/dashboardTypes'
import type {
  Command, Device, DeviceUser, IdentityConflictReport, UserCommandResponse, UserDeletionJob,
} from '../types'
import { plainStatus } from '../words/states'
import { requestFailure } from '../words/requests'
import {
  OLD_CHECKINS, PERSON_DIALOG, REMOVE_MANY, SAME_CNIC, employeeIdList, peopleError, removalHeading, removalItem,
} from '../words/people'
import { DialogError, peopleErrorText } from './people/errors'
import { divisionOf, formatCnicTyping, friendlyName, type PeopleUser } from './people/model'
import type { RetryState } from './people/useCommandTray'

type ToastLike = { notice: (text: string) => void; error: (text: string) => void }
type ErrorText = { sentence: string; raw: string } | null

export type CommandMeta = { name?: string; userKey?: string; retry?: RetryState }

/** The device line every dialog that changes a machine carries (name + Life/Health). */
export function DeviceLine({ device }: { device: Device }) {
  return (
    <p className="people-dialog-device">
      <Icon name="server" size="sm" />
      <span data-user-text>{friendlyName(device)}</span>
      <DivisionTag division={divisionOf(device)} />
    </p>
  )
}

/** "To confirm, type exactly: PHRASE" with the phrase in a code box (kept as it is today). */
function TypedPhraseLabel({ label, phrase, note }: { label: string; phrase: string; note?: string }) {
  return (
    <span className="people-typed-label">
      {label} <code className="people-typed-phrase" data-typed-phrase>{phrase}</code>
      {note && <small> {note}</small>}
    </span>
  )
}

const cnicDigitsOnly = (value: string) => value.replace(/\D/g, '').slice(0, 13)

type Field = 'display_name' | 'cnic' | 'user_id_override' | 'password' | 'reason' | 'confirmation'

export function UserOperationDialog({
  state,
  device,
  onClose,
  onCommand,
  toast,
  draft,
}: {
  state: Exclude<UserDialogState, null>
  device: Device
  onClose: () => void
  onCommand: (command: Command, meta?: CommandMeta) => void
  toast: ToastLike
  /** Values kept from a failed try ("Try again"); the password is never kept. */
  draft?: Record<string, unknown>
}) {
  const user = state.mode === 'create' ? null : state.user as PeopleUser
  const deviceName = friendlyName(device)
  // A suspect CNIC suffix stays out of the name: the CNIC is appended once, within 24 bytes.
  const suspectName = Boolean(state.mode === 'edit' && user?.cnic_format_suspect && !user.cnic_available)
  const [displayName, setDisplayName] = useState(String(draft?.displayName ?? ((suspectName && user?.suggested_display_name) || user?.display_name || '')))
  const [cnic, setCnic] = useState(cnicDigitsOnly(String(draft?.cnic ?? '')))
  // The unparsed name's "-S" marker never set the flag; keep it rather than drop it with the suffix.
  const [shiftWorker, setShiftWorker] = useState(Boolean(draft?.shiftWorker ?? (user?.shift_worker || Boolean(suspectName && user?.suggested_shift_worker))))
  const [privilege, setPrivilege] = useState<0 | 14>((draft?.privilege as 0 | 14 | undefined) ?? user?.privilege ?? 0)
  const [userIdOverride, setUserIdOverride] = useState(String(draft?.userIdOverride ?? ''))
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [elevationReason, setElevationReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ErrorText>(null)
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<Field, string>>>({})
  const [nothingChanged, setNothingChanged] = useState(false)
  const formRef = useRef<HTMLFormElement>(null)
  const moreRef = useRef<HTMLDetailsElement>(null)
  const conflictRequiresCnic = Boolean(state.mode === 'edit' && user?.identity_conflict_code && !user.identity_conflict_resolved)
  const missingCnicRequiresCnic = Boolean(state.mode === 'edit' && user && !user.cnic_available)
  const editRequiresCnic = conflictRequiresCnic || missingCnicRequiresCnic
  const permanentElevation = state.mode === 'edit' && user?.privilege !== 14 && privilege === 14
  const elevationConfirmation = user ? `ELEVATE ${user.user_id} ON ${device.device_id}` : ''
  const nameByteLimit = Number(device.zkt?.capabilities.name_bytes || 24)
  const roleLocked = device.firmware_family === 'hikvision' && device.zkt?.capabilities.user_role_write === false
  const fullCnic = /^\d{13}$/.test(cnic)
  const preview = fullCnic
    ? buildMachinePreview(displayName, cnic, shiftWorker, nameByteLimit)
    : editRequiresCnic || state.mode === 'create'
      ? PERSON_DIALOG.needCnicPreview
      : user?.machine_name_preview || PERSON_DIALOG.cnicHidden
  const canonical = displayName.trim().replace(/\s+/g, ' ')
  const shortened = fullCnic && !preview.startsWith(`${canonical}-`)
  const hasEditChange = Boolean(user && (
    displayName.trim() !== user.display_name || shiftWorker !== user.shift_worker || privilege !== user.privilege || Boolean(cnic)
  ))
  const dirty = Boolean(password || confirmation || elevationReason || cnic || userIdOverride
    || (state.mode === 'create' && displayName.trim()) || (state.mode === 'edit' && hasEditChange))
  const deleteReady = state.mode === 'delete' && confirmationMatches(confirmation, state.user) && Boolean(password)
  const leaseReady = state.mode === 'lease' && Boolean(password)

  useEffect(() => { setNothingChanged(false) }, [displayName, cnic, shiftWorker, privilege])

  const focusField = (field: Field) => {
    if (field === 'user_id_override' && moreRef.current) moreRef.current.open = true
    window.setTimeout(() => formRef.current?.querySelector<HTMLElement>(`[data-field="${field}"]`)?.focus(), 0)
  }

  const validate = (): Partial<Record<Field, string>> => {
    const errors: Partial<Record<Field, string>> = {}
    if (state.mode === 'create' || state.mode === 'edit') {
      if (!displayName.trim()) errors.display_name = PERSON_DIALOG.errName
      if (state.mode === 'create' && !fullCnic) errors.cnic = PERSON_DIALOG.errCnicLength(cnic.length)
      if (state.mode === 'edit' && editRequiresCnic && !cnic) errors.cnic = conflictRequiresCnic ? PERSON_DIALOG.errCnicConflict : PERSON_DIALOG.errCnicMissing
      else if (state.mode === 'edit' && cnic && !fullCnic) errors.cnic = PERSON_DIALOG.errCnicLength(cnic.length)
      if (state.mode === 'create' && userIdOverride && !/^\d+$/.test(userIdOverride)) errors.user_id_override = PERSON_DIALOG.errEmployeeId
      if (permanentElevation && elevationReason.trim().length < 10) errors.reason = PERSON_DIALOG.errReason
      if (permanentElevation && confirmation !== elevationConfirmation) errors.confirmation = PERSON_DIALOG.errPhrase
    }
    if (state.mode === 'delete' && !confirmationMatches(confirmation, state.user)) errors.confirmation = PERSON_DIALOG.errIdOrName
    if (!password) errors.password = PERSON_DIALOG.errPassword
    return errors
  }

  const retryState = (): RetryState => ({ dialog: state, draft: { displayName, cnic, shiftWorker, privilege, userIdOverride } })

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy) return
    setError(null)
    if (state.mode === 'edit' && !hasEditChange) {
      setFieldErrors({})
      setNothingChanged(true)
      return
    }
    const errors = validate()
    setFieldErrors(errors)
    const first = (['display_name', 'cnic', 'user_id_override', 'reason', 'confirmation', 'password'] as Field[]).find((field) => errors[field])
    if (first) {
      focusField(first)
      return
    }
    setBusy(true)
    try {
      if (state.mode === 'create') {
        const name = displayName.trim()
        const response = await api<UserCommandResponse>(`/api/v2/devices/${device.connector_id}/users`, {
          method: 'POST',
          body: JSON.stringify({
            display_name: name,
            cnic,
            shift_worker: shiftWorker,
            user_id_override: userIdOverride || null,
            password,
            idempotency_key: idempotency('create-user'),
          }),
        })
        onCommand(response.command, { name, userKey: response.user?.user_key, retry: retryState() })
        toast.notice(PERSON_DIALOG.toastAdd(name))
      } else if (state.mode === 'edit') {
        const response = await api<UserCommandResponse>(`/api/v2/devices/${device.connector_id}/users/${state.user.user_key}`, {
          method: 'PATCH',
          body: JSON.stringify({
            display_name: displayName.trim(),
            ...(cnic ? { cnic } : {}),
            shift_worker: shiftWorker,
            privilege,
            ...(permanentElevation ? { reason: elevationReason.trim(), typed_confirmation: confirmation } : {}),
            expected_version: state.user.row_version,
            password,
            idempotency_key: idempotency('update-user'),
          }),
        })
        onCommand(response.command, { name: displayName.trim(), userKey: state.user.user_key, retry: retryState() })
        toast.notice(PERSON_DIALOG.toastEdit(displayName.trim()))
      } else if (state.mode === 'delete') {
        const response = await api<UserCommandResponse>(`/api/v2/devices/${device.connector_id}/users/${state.user.user_key}`, {
          method: 'DELETE',
          body: JSON.stringify({
            expected_version: state.user.row_version,
            typed_confirmation: confirmation,
            password,
            idempotency_key: idempotency('delete-user'),
          }),
        })
        onCommand(response.command, { name: state.user.display_name, userKey: state.user.user_key, retry: { dialog: state } })
        toast.notice(PERSON_DIALOG.toastRemove(state.user.display_name))
      } else {
        const response = await api<{ command: Command }>(`/api/v1/devices/${device.connector_id}/admin-leases`, {
          method: 'POST',
          body: JSON.stringify({ uid: state.user.uid, password, idempotency_key: idempotency('enrollment-lease') }),
        })
        onCommand(response.command, { name: state.user.display_name, userKey: state.user.user_key, retry: { dialog: state } })
        toast.notice(PERSON_DIALOG.toastLease(state.user.display_name))
      }
      onClose()
    } catch (reason) {
      if (reason instanceof ApiError) {
        const fromServer: Partial<Record<Field, string>> = {}
        for (const [key, messages] of Object.entries(reason.fieldErrors || {})) {
          if (messages?.[0] && ['display_name', 'cnic', 'user_id_override', 'password', 'reason'].includes(key)) {
            fromServer[key as Field] = peopleErrorText(new ApiError(reason.status, messages[0]), messages[0]).sentence
          }
        }
        setFieldErrors(fromServer)
      }
      setError(peopleErrorText(reason, PERSON_DIALOG.failed))
    } finally {
      setBusy(false)
    }
  }

  const fieldError = (field: Field) => fieldErrors[field]
    ? <small className="field-error" id={`person-${field}-error`}>{fieldErrors[field]}</small>
    : null
  const invalid = (field: Field) => ({
    'aria-invalid': Boolean(fieldErrors[field]),
    'aria-describedby': fieldErrors[field] ? `person-${field}-error` : undefined,
    'data-field': field,
  })

  const title = state.mode === 'create' ? PERSON_DIALOG.addTitle(deviceName)
    : state.mode === 'edit' ? PERSON_DIALOG.editTitle(state.user.display_name)
      : state.mode === 'delete' ? PERSON_DIALOG.removeTitle(state.user.display_name, deviceName)
        : PERSON_DIALOG.leaseTitle
  const description = state.mode === 'create' ? PERSON_DIALOG.addDescription
    : state.mode === 'edit' ? PERSON_DIALOG.editDescription
      : state.mode === 'delete' ? PERSON_DIALOG.removeDescription
        : PERSON_DIALOG.leaseDescription(state.user.display_name, deviceName)
  const cnicLabel = state.mode === 'edit'
    ? conflictRequiresCnic ? PERSON_DIALOG.cnicConflict : missingCnicRequiresCnic ? PERSON_DIALOG.cnicMissing : PERSON_DIALOG.cnicOptional
    : PERSON_DIALOG.cnic
  const editProblem = state.mode === 'edit' && !suspectName
    ? conflictRequiresCnic ? PERSON_DIALOG.problemSameCnic(employeeIdList(user?.identity_conflict_members.map((member) => member.user_id)))
      : missingCnicRequiresCnic ? PERSON_DIALOG.problemNoCnic : ''
    : ''
  const submitLabel = busy
    ? state.mode === 'delete' ? PERSON_DIALOG.removing : state.mode === 'lease' ? PERSON_DIALOG.turningOn : PERSON_DIALOG.sending
    : state.mode === 'delete' ? PERSON_DIALOG.removeButton : state.mode === 'lease' ? PERSON_DIALOG.giveAccess : state.mode === 'create' ? PERSON_DIALOG.add : PERSON_DIALOG.save
  const submitDisabled = busy || (state.mode === 'delete' && !deleteReady) || (state.mode === 'lease' && !leaseReady)

  return (
    <Dialog titleId="user-operation-title" title={title} description={description} onClose={onClose} dirty={dirty} className="people-person-dialog">
      <form ref={formRef} className="dialog-body" onSubmit={submit} noValidate>
        <DeviceLine device={device} />
        {state.mode === 'delete' && device.firmware_family === 'hikvision' && <p className="info-copy">{device.zkt?.capabilities.delete_all_credentials === true ? PERSON_DIALOG.hikFullDelete : PERSON_DIALOG.hikOlder}</p>}
        {(state.mode === 'create' || state.mode === 'edit') && (
          <>
            {suspectName && user && <div className="message pattern-waiting" role="note"><Icon name="alert" /><span>{PERSON_DIALOG.suspectNote(user.display_name, user.suggested_display_name || user.display_name)}</span></div>}
            {editProblem && <div className="message pattern-waiting" role="note"><Icon name="alert" /><span>{editProblem}</span></div>}
            <div className="form-grid">
              <label>{PERSON_DIALOG.fullName}<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} maxLength={255} autoComplete="off" {...invalid('display_name')} />{fieldError('display_name')}</label>
              <label>{cnicLabel}
                <input inputMode="numeric" autoComplete="off" value={formatCnicTyping(cnic)} onChange={(event) => setCnic(cnicDigitsOnly(event.target.value))} placeholder={PERSON_DIALOG.cnicPlaceholder} required={editRequiresCnic} {...invalid('cnic')} />
                <small className="people-cnic-count" aria-live="polite">{cnic ? <>{PERSON_DIALOG.cnicCount(cnic.length)}{fullCnic && <Icon name="check" size="sm" />}</> : null}</small>
                {fieldError('cnic')}
              </label>
            </div>
            <label className="check-field"><input type="checkbox" checked={shiftWorker} onChange={(event) => setShiftWorker(event.target.checked)} /><span><strong>{PERSON_DIALOG.shift}</strong><small>{PERSON_DIALOG.shiftHint}</small></span></label>
            <div className="preview-box people-name-preview">
              <span>{PERSON_DIALOG.machineName}</span>
              <code data-user-text>{preview}</code>
              <small>{fullCnic ? <>{shortened ? PERSON_DIALOG.shortened : PERSON_DIALOG.fits}{!shortened && <Icon name="check" size="sm" />}</> : editRequiresCnic ? PERSON_DIALOG.needCnic : state.mode === 'edit' ? PERSON_DIALOG.cnicHidden : null}</small>
              {fullCnic && <TechnicalDetails>{`${utf8Length(preview)} / ${nameByteLimit} bytes`}</TechnicalDetails>}
            </div>
            <details ref={moreRef} className="people-more-options" open={Boolean(fieldErrors.user_id_override) || permanentElevation || undefined}>
              <summary>{PERSON_DIALOG.moreOptions}</summary>
              {state.mode === 'create' && <label>{PERSON_DIALOG.employeeIdOptional}<input inputMode="numeric" value={userIdOverride} onChange={(event) => setUserIdOverride(event.target.value.replace(/\D/g, '').slice(0, device.firmware_family === 'hikvision' ? 32 : 24))} {...invalid('user_id_override')} /><small>{PERSON_DIALOG.employeeIdHint}</small>{fieldError('user_id_override')}</label>}
              {state.mode === 'edit' && <label>{PERSON_DIALOG.role}<select value={privilege} disabled={roleLocked} onChange={(event) => { setPrivilege(Number(event.target.value) as 0 | 14); setConfirmation(''); setElevationReason('') }}><option value={0}>{PERSON_DIALOG.roleRegular}</option><option value={14}>{PERSON_DIALOG.roleAdmin}</option></select><small>{device.firmware_family === 'hikvision' ? roleLocked ? PERSON_DIALOG.roleLocked : PERSON_DIALOG.roleHik : PERSON_DIALOG.roleHint}</small></label>}
            </details>
          </>
        )}
        {state.mode === 'delete' && (
          <div className="destructive-copy pattern-blocked">
            <Icon name="trash" />
            <div><h3 data-user-text>{state.user.display_name}</h3><p>{`Employee ID ${state.user.user_id}`}</p><p>{PERSON_DIALOG.removeKept}</p></div>
          </div>
        )}
        {state.mode === 'lease' && (
          <div className="info-copy pattern-waiting"><Icon name="clock" /><div><h3>{PERSON_DIALOG.leaseEnds}</h3><p>{PERSON_DIALOG.leaseOnly(deviceName)}</p></div></div>
        )}
        {permanentElevation && (
          <section className="permanent-elevation pattern-blocked">
            <div className="info-copy pattern-blocked"><Icon name="alert" /><div><h3>{PERSON_DIALOG.elevateTitle}</h3><p>{PERSON_DIALOG.elevateBody}</p></div></div>
            <label>{PERSON_DIALOG.why}<textarea value={elevationReason} onChange={(event) => setElevationReason(event.target.value)} maxLength={500} rows={3} {...invalid('reason')} />{fieldError('reason')}</label>
            <label><TypedPhraseLabel label={PERSON_DIALOG.typeExactly} phrase={elevationConfirmation} /><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" spellCheck={false} {...invalid('confirmation')} />{fieldError('confirmation')}</label>
          </section>
        )}
        {state.mode === 'delete' && <label>{PERSON_DIALOG.typeIdOrName(state.user.user_id)}<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" spellCheck={false} {...invalid('confirmation')} />{fieldError('confirmation')}</label>}
        <label>{PERSON_DIALOG.password}<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} {...invalid('password')} />{fieldError('password')}</label>
        <DialogError error={error} />
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>{PERSON_DIALOG.cancel}</button>
          <button className={`button ${state.mode === 'delete' ? 'destructive' : 'primary'}`} type="submit" disabled={submitDisabled} aria-busy={busy || undefined}>{submitLabel}</button>
        </footer>
        {nothingChanged && <p className="people-nothing-changed" role="status">{PERSON_DIALOG.nothingChanged}</p>}
      </form>
    </Dialog>
  )
}

export function IdentityResolutionDialog({
  state,
  device,
  onClose,
  onComplete,
  toast,
}: {
  state: Exclude<IdentityResolutionDialogState, null>
  device: Device
  onClose: () => void
  onComplete: (report: IdentityConflictReport) => void
  toast: ToastLike
}) {
  const resolving = state.mode === 'resolve'
  // Server-checked phrases (be/schemas.py); kept typed until the owner decides (D-19).
  const expectedConfirmation = resolving ? 'SAME EMPLOYEE' : 'REVOKE RESOLUTION'
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ErrorText>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    if (reason.trim().length < 10) return setError({ sentence: SAME_CNIC.errReason, raw: '' })
    if (confirmation !== expectedConfirmation) return setError({ sentence: SAME_CNIC.errPhrase, raw: '' })
    if (!password) return setError({ sentence: SAME_CNIC.errPassword, raw: '' })
    setBusy(true)
    try {
      const path = resolving
        ? `/api/v2/devices/${device.connector_id}/identity-conflicts/resolve`
        : `/api/v2/devices/${device.connector_id}/identity-conflicts/${state.group.resolution_id}/revoke`
      const body = resolving
        ? {
            group_token: state.group.group_token,
            members: state.group.members.map((member) => ({ user_key: member.user_key, expected_version: member.row_version })),
            reason: reason.trim(),
            typed_confirmation: expectedConfirmation,
            password,
            idempotency_key: idempotency('identity-resolution'),
          }
        : { reason: reason.trim(), typed_confirmation: expectedConfirmation, password }
      const response = await api<{ report: IdentityConflictReport }>(path, { method: 'POST', body: JSON.stringify(body) })
      onComplete(response.report)
      toast.notice(resolving ? SAME_CNIC.saved : SAME_CNIC.undone)
      onClose()
    } catch (reasonValue) {
      setError(peopleErrorText(reasonValue, SAME_CNIC.failed))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      titleId="identity-resolution-title"
      title={resolving ? SAME_CNIC.dialogTitle : SAME_CNIC.dialogUndoTitle}
      description={resolving ? SAME_CNIC.dialogDescription : SAME_CNIC.dialogUndoDescription}
      onClose={onClose}
      dirty={Boolean(reason || confirmation || password)}
    >
      <form className="dialog-body" onSubmit={submit}>
        <DeviceLine device={device} />
        <div className={resolving ? 'info-copy pattern-waiting' : 'destructive-copy pattern-blocked'}>
          <Icon name={resolving ? 'shield' : 'alert'} />
          <div>
            <h3>{SAME_CNIC.cnic} {state.group.cnic_masked}</h3>
            <ul className="people-member-list">{state.group.members.map((member) => <li key={member.user_key} data-user-text>{SAME_CNIC.member(member.display_name, member.user_id)}</li>)}</ul>
            <p>{SAME_CNIC.nothingChanges}</p>
          </div>
        </div>
        <label>{SAME_CNIC.why}<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} rows={3} /></label>
        <label><TypedPhraseLabel label={SAME_CNIC.typeExactly} phrase={expectedConfirmation} /><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" spellCheck={false} /></label>
        <label>{SAME_CNIC.password}<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        <DialogError error={error} />
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>{SAME_CNIC.cancel}</button>
          <button className={`button ${resolving ? 'primary' : 'destructive'}`} disabled={busy}>{busy ? SAME_CNIC.saving : resolving ? SAME_CNIC.confirmButton : SAME_CNIC.undoButton}</button>
        </footer>
      </form>
    </Dialog>
  )
}

export function HistoricalIdentityResolutionDialog({
  state,
  device,
  onClose,
  onComplete,
  toast,
  simple = false,
}: {
  simple?: boolean
  state: Exclude<HistoricalIdentityDialogState, null>
  device: Device
  onClose: () => void
  onComplete: () => Promise<void>
  toast: Pick<ToastLike, 'notice' | 'error'> & { show?: (input: { tone: 'success' | 'info' | 'error'; text: string; action?: { label: string; onClick: () => void } }) => void }
}) {
  const candidate = state.candidate
  const requestKey = useRef(idempotency('historical-identity-repair'))
  const [cnic, setCnic] = useState('')
  const [employeeId, setEmployeeId] = useState('')
  const [serviceNumber, setServiceNumber] = useState(candidate.user_id)
  const [employeeName, setEmployeeName] = useState(candidate.display_name)
  const [zoneCode, setZoneCode] = useState(device.zone_id)
  const [reason, setReason] = useState('')
  const [verified, setVerified] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ErrorText>(null)
  const currentIdentityEvidence = candidate.resolution_path === 'CURRENT_IDENTITY_EVIDENCE'
  const expectedConfirmation = currentIdentityEvidence
    ? `${candidate.user_id} -> CURRENT ${candidate.user_id}`
    : `${candidate.user_id} -> HR ${employeeId}`
  const fail = (sentence: string) => setError({ sentence, raw: '' })

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    if (!/^\d{13}$/.test(cnic)) return fail(OLD_CHECKINS.errCnic)
    if (!currentIdentityEvidence) {
      if (!/^\d+$/.test(employeeId)) return fail(OLD_CHECKINS.errHrId)
      if (!/^[A-Za-z0-9._-]+$/.test(serviceNumber)) return fail(OLD_CHECKINS.errService)
    }
    if (!employeeName.trim()) return fail(OLD_CHECKINS.errName)
    if (reason.trim().length < 10) return fail(OLD_CHECKINS.errReason)
    if (simple ? !verified : confirmation !== expectedConfirmation) return fail(simple ? OLD_CHECKINS.errTick : OLD_CHECKINS.errPhrase)
    if (!password) return fail(OLD_CHECKINS.errPassword)
    if (candidate.source_kind === 'EVENT_GROUP' && !candidate.group_token) return fail(OLD_CHECKINS.errChanged)
    if (currentIdentityEvidence && (!candidate.active_user_key || candidate.active_user_row_version == null)) return fail(OLD_CHECKINS.errChanged)
    if (candidate.source_kind !== 'EVENT_GROUP' && (!candidate.source_user_key || candidate.row_version == null)) return fail(OLD_CHECKINS.errChanged)
    setBusy(true)
    try {
      const eventGroup = candidate.source_kind === 'EVENT_GROUP'
      const endpoint = currentIdentityEvidence ? 'resolve-current-identity' : eventGroup ? 'resolve-event-group' : 'resolve'
      await api<{ repaired_events: number }>(`/api/v2/devices/${device.connector_id}/historical-identities/${endpoint}`, {
        method: 'POST',
        body: JSON.stringify({
          ...(currentIdentityEvidence
            ? {
                group_token: candidate.group_token,
                source_user_id: candidate.user_id,
                source_uid: candidate.uid,
                target_user_key: candidate.active_user_key,
                expected_version: candidate.active_user_row_version,
                verified_employee_name: employeeName.trim(),
              }
            : eventGroup
              ? { group_token: candidate.group_token, source_user_id: candidate.user_id, source_uid: candidate.uid }
              : { source_user_key: candidate.source_user_key, expected_version: candidate.row_version }),
          source_cnic: cnic,
          ...(!currentIdentityEvidence
            ? {
                directory_employee_id: employeeId,
                directory_service_number: serviceNumber,
                directory_employee_name: employeeName.trim(),
                directory_zone_code: zoneCode.trim() || null,
              }
            : {}),
          reason: reason.trim(),
          typed_confirmation: simple ? expectedConfirmation : confirmation,
          password,
          idempotency_key: requestKey.current,
        }),
      })
      await onComplete()
      const next = `/attendance?view=fix-tools&tool=held-staff&device_id=${encodeURIComponent(device.connector_id)}`
      if (toast.show) toast.show({ tone: 'success', text: OLD_CHECKINS.saved, action: { label: OLD_CHECKINS.open, onClick: () => { window.location.assign(next) } } })
      else toast.notice(OLD_CHECKINS.saved)
      onClose()
    } catch (reasonValue) {
      setError(peopleErrorText(reasonValue, OLD_CHECKINS.failed))
    } finally {
      setBusy(false)
    }
  }

  const range = candidate.first_event_at && candidate.last_event_at ? OLD_CHECKINS.range(candidate.event_count, day(candidate.first_event_at), day(candidate.last_event_at)) : ''
  return (
    <Dialog
      titleId="historical-identity-resolution-title"
      title={currentIdentityEvidence ? OLD_CHECKINS.dialogSame : OLD_CHECKINS.dialogHr}
      description={currentIdentityEvidence ? OLD_CHECKINS.dialogSameDescription : OLD_CHECKINS.dialogHrDescription}
      onClose={onClose}
      dirty={Boolean(cnic || employeeId || reason || confirmation || password || verified)}
    >
      <form className="dialog-body" onSubmit={submit}>
        <div className="info-copy pattern-waiting">
          <Icon name="shield" />
          <div>
            <h3 data-user-text>{candidate.display_name}</h3>
            <p>{OLD_CHECKINS.who(candidate.user_id, friendlyName(device))}</p>
            {range && <p>{range}</p>}
          </div>
        </div>
        <div className="form-grid">
          <label>{OLD_CHECKINS.cnic}<input inputMode="numeric" autoComplete="off" value={formatCnicTyping(cnic)} onChange={(event) => setCnic(cnicDigitsOnly(event.target.value))} placeholder={PERSON_DIALOG.cnicPlaceholder} /></label>
          {!currentIdentityEvidence && <label>{OLD_CHECKINS.hrId}<input inputMode="numeric" value={employeeId} onChange={(event) => setEmployeeId(event.target.value.replace(/\D/g, '').slice(0, 32))} /><small>{OLD_CHECKINS.hrIdHelp(candidate.user_id)}</small></label>}
          {!currentIdentityEvidence && <label>{OLD_CHECKINS.service}<input value={serviceNumber} onChange={(event) => setServiceNumber(event.target.value.replace(/[^A-Za-z0-9._-]/g, '').slice(0, 64))} /></label>}
          <label>{OLD_CHECKINS.name}<input value={employeeName} onChange={(event) => setEmployeeName(event.target.value)} maxLength={255} /></label>
          {!currentIdentityEvidence && <label>{OLD_CHECKINS.zone}<input value={zoneCode} onChange={(event) => setZoneCode(event.target.value)} maxLength={64} /></label>}
        </div>
        <div className="message pattern-waiting" role="note">
          <Icon name="alert" />
          {currentIdentityEvidence ? OLD_CHECKINS.sameNote : OLD_CHECKINS.hrNote}
        </div>
        <label>{OLD_CHECKINS.how}<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} rows={3} /></label>
        {simple
          ? <label className="check-field"><input type="checkbox" checked={verified} onChange={(event) => setVerified(event.target.checked)} /><span><strong>{OLD_CHECKINS.checked}</strong></span></label>
          : <label><TypedPhraseLabel label={OLD_CHECKINS.typeExactly} phrase={expectedConfirmation} /><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" spellCheck={false} /></label>}
        <label>{OLD_CHECKINS.password}<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        <DialogError error={error} />
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>{OLD_CHECKINS.cancel}</button>
          <button className="button primary" disabled={busy}>{busy ? OLD_CHECKINS.saving : currentIdentityEvidence ? OLD_CHECKINS.save : OLD_CHECKINS.saveDetails}</button>
        </footer>
      </form>
    </Dialog>
  )
}

export function BulkDeletionDialog({
  users,
  device,
  onRevalidate,
  onClose,
  onCreated,
}: {
  users: DeviceUser[]
  device: Device
  onRevalidate: () => Promise<{ users: DeviceUser[]; changed: boolean }>
  onClose: () => void
  onCreated: (job: UserDeletionJob) => void
}) {
  // The server's phrase, unchanged (be/service.py): it names this one device.
  const expectedConfirmation = bulkDeletionConfirmation(users.length, device.device_id)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ErrorText>(null)
  const fail = (sentence: string) => setError({ sentence, raw: '' })
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    if (reason.trim().length < 10) return fail(REMOVE_MANY.errReason)
    if (confirmation !== expectedConfirmation) return fail(REMOVE_MANY.errPhrase)
    if (!password) return fail(REMOVE_MANY.errPassword)
    setBusy(true)
    try {
      const validation = await onRevalidate()
      if (validation.changed) {
        fail(validation.users.length ? REMOVE_MANY.listChanged : REMOVE_MANY.noneNow)
        return
      }
      const response = await api<{ job: UserDeletionJob }>(`/api/v2/devices/${device.connector_id}/user-deletion-jobs`, {
        method: 'POST',
        body: JSON.stringify({
          targets: validation.users.map((user) => ({ user_key: user.user_key, expected_version: user.row_version })),
          reason: reason.trim(),
          typed_confirmation: confirmation,
          password,
          idempotency_key: idempotency('bulk-delete-users'),
        }),
      })
      onCreated(response.job)
      onClose()
    } catch (reasonValue) {
      setError(peopleErrorText(reasonValue, REMOVE_MANY.startFailed))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog
      titleId="bulk-user-deletion-title"
      title={REMOVE_MANY.title(users.length, friendlyName(device))}
      description={REMOVE_MANY.description}
      onClose={onClose}
      className="bulk-deletion-dialog"
      dirty={Boolean(reason || confirmation || password)}
    >
      <form className="dialog-body" onSubmit={submit}>
        <DeviceLine device={device} />
        <ul className="people-member-list is-scroll" aria-label={REMOVE_MANY.title(users.length, friendlyName(device))}>
          {users.map((user) => <li key={user.user_key} data-user-text>{REMOVE_MANY.member(user.display_name, user.user_id)}</li>)}
        </ul>
        <p>{REMOVE_MANY.kept}</p>
        <label>{REMOVE_MANY.why}<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} rows={3} /></label>
        <label><TypedPhraseLabel label={REMOVE_MANY.typeExactly} phrase={expectedConfirmation} note={REMOVE_MANY.phraseNote(device.device_id)} /><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" spellCheck={false} /></label>
        <label>{REMOVE_MANY.password}<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        <DialogError error={error} />
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>{REMOVE_MANY.cancel}</button>
          <button className="button destructive" disabled={busy}>{busy ? REMOVE_MANY.starting : REMOVE_MANY.submit(users.length)}</button>
        </footer>
      </form>
    </Dialog>
  )
}

const ACTIVE_JOB = ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED']

const itemReason = (item: UserDeletionJob['items'][number]) => {
  if (item.error_code) {
    const coded = requestFailure(item.error_code)
    if (!coded.unknown) return coded.sentence.replace(/\.$/, '')
  }
  return peopleError(item.error_message)?.replace(/\.$/, '') || null
}
export const isActiveRemoval = (job: UserDeletionJob | null | undefined) => Boolean(job && ACTIVE_JOB.includes(job.status))

/** Removing several people: progress bar, person-by-person list, Stop (password kept) and Clear. */
export function BulkDeletionProgress({
  job,
  deviceName,
  onCancel,
  onClear,
}: {
  job: UserDeletionJob
  deviceName?: string
  onCancel: (password: string) => Promise<void>
  onClear?: () => void
}) {
  const active = isActiveRemoval(job)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const done = job.counts.succeeded + job.counts.failed + job.counts.canceled + job.counts.expired
  const total = Math.max(job.counts.requested, job.items.length, 1)
  const word = plainStatus('peopleRemoval', job.status)
  const items: ReactNode = job.items.length > 0 && (
    <ul className="people-removal-items">
      {job.items.map((item) => {
        const status = item.status.toUpperCase()
        const icon = status === 'SUCCEEDED' ? 'check' : status === 'FAILED' ? 'x' : status === 'CANCELED' || status === 'CANCELLED' || status === 'EXPIRED' ? 'pause' : 'clock'
        return (
          <li key={item.user_key} data-item-status={status}>
            <Icon name={icon} size="sm" />
            <span><span data-user-text>{item.display_name}</span> — {removalItem(status, status === 'FAILED' ? itemReason(item) : null)}</span>
            {(item.error_code || item.error_message) && <TechnicalDetails>{[item.user_id, item.error_code, item.error_message].filter(Boolean).join(' · ')}</TechnicalDetails>}
          </li>
        )
      })}
    </ul>
  )
  return (
    <section className={`people-removal-card is-${word.tone}`} aria-live="polite" aria-label={REMOVE_MANY.progressTitle(deviceName || '')}>
      <p className="people-removal-title">{REMOVE_MANY.progressTitle(deviceName || '')}</p>
      <p className="people-removal-heading">{removalHeading(job.status, job.counts)}</p>
      <div className="people-removal-bar" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done} aria-label={REMOVE_MANY.progressDone(done, total)}>
        <span style={{ width: `${Math.round((done / total) * 100)}%` }} />
      </div>
      <p className="people-muted">{REMOVE_MANY.progressCounts(job.counts.succeeded, job.counts.pending, job.counts.failed, job.counts.canceled + job.counts.expired)}</p>
      {items}
      <div className="people-removal-actions">
        {active && job.status !== 'CANCEL_REQUESTED' && (
          !cancelOpen
            ? <button type="button" className="button secondary small" onClick={() => setCancelOpen(true)}>{REMOVE_MANY.stop}</button>
            : (
              <form className="people-removal-stop" onSubmit={async (event) => {
                event.preventDefault()
                if (!password || busy) return
                setBusy(true)
                try { await onCancel(password) } catch { /* the page shows the error */ } finally { setBusy(false) }
              }}>
                <label><span className="sr-only">{REMOVE_MANY.stopPassword}</span><input aria-label={REMOVE_MANY.stopPassword} type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder={REMOVE_MANY.stopPassword} /></label>
                <button className="button destructive small" type="submit" disabled={busy || !password}>{busy ? REMOVE_MANY.stopping : REMOVE_MANY.stopConfirm}</button>
              </form>
            )
        )}
        {!active && onClear && <button type="button" className="button secondary small" onClick={onClear}>{REMOVE_MANY.clearCard}</button>}
      </div>
      <TechnicalDetails>{[job.job_id, job.status].join(' · ')}</TechnicalDetails>
    </section>
  )
}
