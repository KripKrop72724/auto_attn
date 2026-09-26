import { useRef, useState, type FormEvent } from 'react'
import { api, ApiError } from '../api'
import {
  Dialog, buildMachinePreview, bulkDeletionConfirmation, confirmationMatches, dateTime,
  idempotency, statusPattern, useToast, utf8Length, validateUserDraft,
  type HistoricalIdentityDialogState, type IdentityResolutionDialogState,
  type UserDialogState,
} from '../App'
import { Icon } from '../Icon'
import type {
  Command, Device, DeviceUser, IdentityConflictReport, UserCommandResponse, UserDeletionJob,
} from '../types'

export function UserOperationDialog({
  state,
  device,
  onClose,
  onCommand,
  toast,
}: {
  state: Exclude<UserDialogState, null>
  device: Device
  onClose: () => void
  onCommand: (command: Command) => void
  toast: ReturnType<typeof useToast>
}) {
  const user = state.mode === 'create' ? null : state.user
  const [displayName, setDisplayName] = useState(user?.display_name || '')
  const [cnic, setCnic] = useState('')
  const [shiftWorker, setShiftWorker] = useState(user?.shift_worker || false)
  const [privilege, setPrivilege] = useState<0 | 14>(user?.privilege || 0)
  const [userIdOverride, setUserIdOverride] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [elevationReason, setElevationReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [fieldErrors, setFieldErrors] = useState<Record<string, string[]>>({})
  const conflictRequiresCnic = Boolean(
    state.mode === 'edit' && user?.identity_conflict_code && !user.identity_conflict_resolved,
  )
  const missingCnicRequiresCnic = Boolean(
    state.mode === 'edit' && user && !user.cnic_available,
  )
  const editRequiresCnic = conflictRequiresCnic || missingCnicRequiresCnic
  const permanentElevation = state.mode === 'edit' && user?.privilege !== 14 && privilege === 14
  const elevationConfirmation = user ? `ELEVATE ${user.user_id} ON ${device.device_id}` : ''
  const nameByteLimit = Number(device.zkt?.capabilities.name_bytes || 24)
  const preview = cnic
    ? buildMachinePreview(displayName, cnic, shiftWorker, nameByteLimit)
    : editRequiresCnic
      ? 'Enter the verified CNIC to generate a safe terminal preview.'
      : user?.machine_name_preview || 'CNIC is preserved and never returned to the browser.'
  const hasEditChange = Boolean(
    user && (
      displayName.trim() !== user.display_name ||
      shiftWorker !== user.shift_worker ||
      privilege !== user.privilege ||
      Boolean(cnic)
    ),
  )
  const canSubmit = !busy && Boolean(password) && (
    state.mode === 'create'
      ? Boolean(displayName.trim()) && /^\d{13}$/.test(cnic) && (!userIdOverride || /^\d+$/.test(userIdOverride))
      : state.mode === 'edit'
        ? hasEditChange && Boolean(displayName.trim()) && (!cnic || /^\d{13}$/.test(cnic)) && (!editRequiresCnic || /^\d{13}$/.test(cnic)) && (!permanentElevation || (elevationReason.trim().length >= 10 && confirmation === elevationConfirmation))
        : state.mode === 'delete'
          ? confirmationMatches(confirmation, state.user)
          : true
  )

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    setFieldErrors({})
    if (state.mode === 'create') {
      const validation = validateUserDraft({ displayName, cnic, password, userIdOverride })
      if (validation) return setError(validation)
    } else if (state.mode === 'edit' && !displayName.trim()) {
      return setError('Full name is required.')
    } else if (state.mode === 'edit' && editRequiresCnic && !cnic) {
      return setError(conflictRequiresCnic ? 'A replacement CNIC is required to resolve this identity conflict.' : 'A verified CNIC is required for this employee.')
    } else if (state.mode === 'edit' && cnic && !/^\d{13}$/.test(cnic)) {
      return setError('CNIC must contain exactly 13 digits.')
    } else if (!password) {
      return setError('Password confirmation is required.')
    } else if (permanentElevation && elevationReason.trim().length < 10) {
      return setError('Record a reason of at least 10 characters for permanent administrator elevation.')
    } else if (permanentElevation && confirmation !== elevationConfirmation) {
      return setError(`Type ${elevationConfirmation} exactly to elevate this user permanently.`)
    } else if (state.mode === 'delete' && !confirmationMatches(confirmation, state.user)) {
      return setError('Type the exact full name or user ID to confirm deletion.')
    }
    setBusy(true)
    try {
      if (state.mode === 'create') {
        const response = await api<UserCommandResponse>(`/api/v2/devices/${device.connector_id}/users`, {
          method: 'POST',
          body: JSON.stringify({
            display_name: displayName.trim(),
            cnic,
            shift_worker: shiftWorker,
            user_id_override: userIdOverride || null,
            password,
            idempotency_key: idempotency('create-user'),
          }),
        })
        onCommand(response.command)
        toast.notice('User creation is queued and will be verified by a terminal reread.')
      } else if (state.mode === 'edit') {
        const response = await api<UserCommandResponse>(
          `/api/v2/devices/${device.connector_id}/users/${state.user.user_key}`,
          {
            method: 'PATCH',
            body: JSON.stringify({
              display_name: displayName.trim(),
              ...(cnic ? { cnic } : {}),
              shift_worker: shiftWorker,
              privilege,
              ...(permanentElevation ? {
                reason: elevationReason.trim(),
                typed_confirmation: confirmation,
              } : {}),
              expected_version: state.user.row_version,
              password,
              idempotency_key: idempotency('update-user'),
            }),
          },
        )
        onCommand(response.command)
        toast.notice('User update is queued with optimistic version checks.')
      } else if (state.mode === 'delete') {
        const response = await api<UserCommandResponse>(
          `/api/v2/devices/${device.connector_id}/users/${state.user.user_key}`,
          {
            method: 'DELETE',
            body: JSON.stringify({
              expected_version: state.user.row_version,
              typed_confirmation: confirmation,
              password,
              idempotency_key: idempotency('delete-user'),
            }),
          },
        )
        onCommand(response.command)
        toast.notice('Deletion is queued. Attendance counts must remain unchanged.')
      } else {
        const response = await api<{ command: Command }>(`/api/v1/devices/${device.connector_id}/admin-leases`, {
          method: 'POST',
          body: JSON.stringify({
            uid: state.user.uid,
            password,
            idempotency_key: idempotency('enrollment-lease'),
          }),
        })
        onCommand(response.command)
        toast.notice('Temporary administrator access is queued for 10 minutes.')
      }
      onClose()
    } catch (reason) {
      if (reason instanceof ApiError) setFieldErrors(reason.fieldErrors)
      setError(reason instanceof Error ? reason.message : 'The operation could not be queued.')
    } finally {
      setBusy(false)
    }
  }

  const copy = {
    create: ['Add user to selected terminal', 'Creates a regular user on this terminal only.'],
    edit: ['Edit device user', 'Name, replacement CNIC, shift status, and permanent role are verified after write.'],
    delete: ['Delete user from terminal', 'The user is removed; punches and identity history remain permanently preserved.'],
    lease: ['Grant enrollment access', 'The selected regular user becomes administrator for 10 minutes, then reverts automatically.'],
  } as const
  const [title, description] = copy[state.mode]
  return (
    <Dialog titleId="user-operation-title" title={title} description={description} onClose={onClose}>
      <form className="dialog-body" onSubmit={submit}>
        {state.mode === 'delete' && device.firmware_family === 'hikvision' && <p className="info-copy">{device.zkt?.capabilities.delete_all_credentials === true
          ? 'Deletes the entire terminal profile, including all enrolled fingerprints, PIN/password, cards, and faces. Those credentials will no longer authenticate this user. Attendance and identity history are retained.'
          : 'This connector has older firmware that restricts deletion of enrolled fingerprints or cards. Update the Hikvision connector firmware to delete the entire profile and its credentials.'}</p>}
        {(state.mode === 'create' || state.mode === 'edit') && (
          <>
            <div className="form-grid">
              <label>Full canonical name<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} maxLength={255} aria-invalid={Boolean(fieldErrors.display_name)} />{fieldErrors.display_name?.[0] && <small className="field-error">{fieldErrors.display_name[0]}</small>}</label>
              <label>{state.mode === 'edit' ? conflictRequiresCnic ? 'Replacement CNIC (required to resolve conflict)' : missingCnicRequiresCnic ? 'Replacement CNIC (required for missing CNIC)' : 'Replacement CNIC (leave blank to preserve)' : 'CNIC'}<input inputMode="numeric" autoComplete="off" value={cnic} onChange={(event) => setCnic(event.target.value.replace(/\D/g, '').slice(0, 13))} placeholder="13 digits" required={editRequiresCnic} aria-invalid={Boolean(fieldErrors.cnic)} />{fieldErrors.cnic?.[0] && <small className="field-error">{fieldErrors.cnic[0]}</small>}</label>
              {state.mode === 'create' && <label>Employee/user ID override (optional)<input inputMode="numeric" value={userIdOverride} onChange={(event) => setUserIdOverride(event.target.value.replace(/\D/g, '').slice(0, device.firmware_family === 'hikvision' ? 32 : 24))} aria-invalid={Boolean(fieldErrors.user_id_override)} />{fieldErrors.user_id_override?.[0] && <small className="field-error">{fieldErrors.user_id_override[0]}</small>}</label>}
              {state.mode === 'edit' && <label>Terminal role<select value={privilege} onChange={(event) => { setPrivilege(Number(event.target.value) as 0 | 14); setConfirmation(''); setElevationReason('') }}><option value={0}>Regular user</option><option value={14}>Permanent administrator</option></select><small>{device.firmware_family === 'hikvision' ? 'Changes local terminal administrator access. Temporary enrollment leases are unavailable on Hikvision.' : 'Prefer the separate 10-minute enrollment lease for routine fingerprint enrollment.'}</small></label>}
            </div>
            <label className="check-field"><input type="checkbox" checked={shiftWorker} onChange={(event) => setShiftWorker(event.target.checked)} /><span><strong>Shift worker</strong><small>Adds the -S- identity marker used for raw-punch handling.</small></span></label>
            <div className="preview-box"><span>Exact terminal name preview ({nameByteLimit} bytes)</span><code>{preview}</code><small>{cnic ? `${utf8Length(preview)} / ${nameByteLimit} UTF-8 bytes` : editRequiresCnic ? 'Verified CNIC is required before this update can be queued.' : 'Stored CNIC remains write-only.'}</small></div>
          </>
        )}
        {state.mode === 'delete' && (
          <div className="destructive-copy pattern-blocked">
            <Icon name="trash" />
            <div><h3>{state.user.display_name}</h3><p>UID {state.user.uid} · User ID {state.user.user_id}</p><p>ADD and terminal attendance records will not be deleted.</p></div>
          </div>
        )}
        {state.mode === 'lease' && (
          <div className="info-copy pattern-waiting"><Icon name="clock" /><div><h3>10-minute automatic lease</h3><p>{state.user.display_name} will be elevated only on {device.display_name}. The ESP watchdog revokes access even if ADD disconnects.</p></div></div>
        )}
        {permanentElevation && <section className="permanent-elevation pattern-blocked"><div className="info-copy pattern-blocked"><Icon name="alert" /><div><h3>Permanent administrator elevation</h3><p>This does not expire automatically. Use it only when a ten-minute enrollment lease cannot meet the documented operational need.</p></div></div><label>Audited elevation reason<textarea value={elevationReason} onChange={(event) => setElevationReason(event.target.value)} maxLength={500} rows={3} /></label><label>Type “{elevationConfirmation}”<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label></section>}
        {state.mode === 'delete' && <label>Type “{state.user.display_name}” or “{state.user.user_id}”<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label>}
        <label>Confirm administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>Cancel</button>
          <button className={`button ${state.mode === 'delete' ? 'destructive' : 'primary'}`} disabled={!canSubmit}>{busy ? 'Queuing…' : state.mode === 'delete' ? 'Delete user safely' : state.mode === 'lease' ? 'Grant 10-minute access' : 'Confirm operation'}</button>
        </footer>
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
  toast: ReturnType<typeof useToast>
}) {
  const resolving = state.mode === 'resolve'
  const expectedConfirmation = resolving ? 'SAME EMPLOYEE' : 'REVOKE RESOLUTION'
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (reason.trim().length < 10) return setError('Record a reason of at least 10 characters.')
    if (confirmation !== expectedConfirmation) {
      return setError(`Type ${expectedConfirmation} exactly to continue.`)
    }
    if (!password) return setError('Password confirmation is required.')
    setBusy(true)
    try {
      const path = resolving
        ? `/api/v2/devices/${device.connector_id}/identity-conflicts/resolve`
        : `/api/v2/devices/${device.connector_id}/identity-conflicts/${state.group.resolution_id}/revoke`
      const body = resolving
        ? {
            group_token: state.group.group_token,
            members: state.group.members.map((member) => ({
              user_key: member.user_key,
              expected_version: member.row_version,
            })),
            reason: reason.trim(),
            typed_confirmation: expectedConfirmation,
            password,
            idempotency_key: idempotency('identity-resolution'),
          }
        : {
            reason: reason.trim(),
            typed_confirmation: expectedConfirmation,
            password,
          }
      const response = await api<{ report: IdentityConflictReport }>(path, {
        method: 'POST',
        body: JSON.stringify(body),
      })
      onComplete(response.report)
      toast.notice(
        resolving
          ? 'Same-employee alias approved. No terminal user or attendance row was changed.'
          : 'Identity resolution revoked. New punches return to identity quarantine.',
      )
      onClose()
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : 'Resolution could not be saved.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      titleId="identity-resolution-title"
      title={resolving ? 'Verify same employee' : 'Revoke identity resolution'}
      description={
        resolving
          ? 'Approve only after the member records are confirmed to belong to one legal identity.'
          : 'Re-enable quarantine if the prior same-employee decision was incorrect.'
      }
      onClose={onClose}
    >
      <form className="dialog-body" onSubmit={submit}>
        <div className={resolving ? 'info-copy pattern-waiting' : 'destructive-copy pattern-blocked'}>
          <Icon name={resolving ? 'shield' : 'alert'} />
          <div>
            <h3>{state.group.cnic_masked || 'Masked CNIC group'}</h3>
            <p>{state.group.members.map((member) => `${member.display_name} · User ${member.user_id}`).join(' | ')}</p>
            <p>No ZKT user, fingerprint template, UID, or attendance event is merged, deleted, or rewritten.</p>
          </div>
        </div>
        <label>Audit reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} rows={3} /></label>
        <label>Type “{expectedConfirmation}”<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label>
        <label>Confirm administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>Cancel</button>
          <button className={`button ${resolving ? 'primary' : 'destructive'}`} disabled={busy}>{busy ? 'Saving…' : resolving ? 'Approve same employee' : 'Revoke resolution'}</button>
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
  toast: Pick<ReturnType<typeof useToast>, 'notice' | 'error'>
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
  const [error, setError] = useState('')
  const currentIdentityEvidence =
    candidate.resolution_path === 'CURRENT_IDENTITY_EVIDENCE'
  const expectedConfirmation = currentIdentityEvidence
    ? `${candidate.user_id} -> CURRENT ${candidate.user_id}`
    : `${candidate.user_id} -> HR ${employeeId}`

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (!/^\d{13}$/.test(cnic)) return setError('Directory CNIC must contain exactly 13 digits.')
    if (!currentIdentityEvidence) {
      if (!/^\d+$/.test(employeeId)) return setError('Directory employee ID must contain only digits.')
      if (!/^[A-Za-z0-9._-]+$/.test(serviceNumber)) {
        return setError('Directory service number contains unsupported characters.')
      }
    }
    if (!employeeName.trim()) return setError('Directory employee name is required.')
    if (reason.trim().length < 10) return setError('Record an audit reason of at least 10 characters.')
    if (simple ? !verified : confirmation !== expectedConfirmation) {
      return setError(simple ? 'Confirm that you verified these employee details.' : `Type ${expectedConfirmation} exactly to continue.`)
    }
    if (!password) return setError('Password confirmation is required.')
    if (
      candidate.source_kind === 'EVENT_GROUP' &&
      !candidate.group_token
    ) {
      return setError('The exact historical event cohort is incomplete. Refresh and retry.')
    }
    if (
      currentIdentityEvidence &&
      (!candidate.active_user_key || candidate.active_user_row_version == null)
    ) {
      return setError('The current terminal identity changed or is incomplete. Refresh and retry.')
    }
    if (
      candidate.source_kind !== 'EVENT_GROUP' &&
      (!candidate.source_user_key || candidate.row_version == null)
    ) {
      return setError('The historical terminal user is incomplete. Refresh and retry.')
    }
    setBusy(true)
    try {
      const eventGroup = candidate.source_kind === 'EVENT_GROUP'
      const endpoint = currentIdentityEvidence
        ? 'resolve-current-identity'
        : eventGroup
          ? 'resolve-event-group'
          : 'resolve'
      await api<{ repaired_events: number }>(
        `/api/v2/devices/${device.connector_id}/historical-identities/${endpoint}`,
        {
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
              ? {
                  group_token: candidate.group_token,
                  source_user_id: candidate.user_id,
                  source_uid: candidate.uid,
                }
              : {
                  source_user_key: candidate.source_user_key,
                  expected_version: candidate.row_version,
                }),
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
        },
      )
      await onComplete()
      toast.notice('Identity evidence saved. Held attendance still requires manual approval in Force release attendance.')
      onClose()
    } catch (reasonValue) {
      setError(
        reasonValue instanceof Error
          ? reasonValue.message
          : 'Historical identity evidence could not be saved.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      titleId="historical-identity-resolution-title"
      title={simple ? 'Verify employee details' : currentIdentityEvidence ? 'Verify preserved cohort against current identity' : 'Enter verified HR identity evidence'}
      description={simple ? 'Save verified employee evidence. Held attendance requires a separate Force release approval.' : currentIdentityEvidence
        ? 'Use the authoritative Oracle capture identity to confirm this exact historical cohort belongs to the unchanged current terminal user.'
        : 'Use authoritative HR directory evidence only. Saving evidence preserves attendance; release requires a separate Force release approval.'}
      onClose={onClose}
    >
      <form className="dialog-body" onSubmit={submit}>
        <div className="info-copy pattern-waiting">
          <Icon name="shield" />
          <div>
            <h3>{candidate.display_name}</h3>
            <p>Employee number {candidate.user_id} · {device.display_name}</p>
            <p>{candidate.event_count.toLocaleString()} preserved events from {dateTime(candidate.first_event_at)} to {dateTime(candidate.last_event_at)}.</p>
          </div>
        </div>
        <div className="form-grid">
          <label>{simple ? 'Verified CNIC' : 'Authoritative CNIC'}<input inputMode="numeric" autoComplete="off" value={cnic} onChange={(event) => setCnic(event.target.value.replace(/\D/g, '').slice(0, 13))} placeholder="13 digits" /></label>
          {!currentIdentityEvidence && <label>HR employee ID<input inputMode="numeric" value={employeeId} onChange={(event) => setEmployeeId(event.target.value.replace(/\D/g, '').slice(0, 32))} /></label>}
          {!currentIdentityEvidence && <label>HR service number<input value={serviceNumber} onChange={(event) => setServiceNumber(event.target.value.replace(/[^A-Za-z0-9._-]/g, '').slice(0, 64))} /></label>}
          <label>{currentIdentityEvidence ? 'Authoritative employee name' : 'HR employee name'}<input value={employeeName} onChange={(event) => setEmployeeName(event.target.value)} maxLength={255} /></label>
          {!currentIdentityEvidence && <label>HR zone code<input value={zoneCode} onChange={(event) => setZoneCode(event.target.value)} maxLength={64} /></label>}
        </div>
        <div className="message pattern-blocked" role="note">
          <Icon name="alert" />
          {currentIdentityEvidence
            ? 'Use the verified employee record. ADD checks that these punches belong to this employee on this device.'
            : 'Do not infer or guess a CNIC. The terminal service number and employee name must match the authoritative HR record.'}
        </div>
        <label>{simple ? 'How did you verify this?' : 'Audit reason'}<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} rows={3} /></label>
        <label>{simple ? <><input type="checkbox" checked={verified} onChange={event => setVerified(event.target.checked)} /> I verified these details belong to the person who made these punches.</> : <>Type “{expectedConfirmation}”<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></>}</label>
        <label>Confirm administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>Cancel</button>
          <button className="button primary" disabled={busy}>{busy ? 'Saving evidence…' : currentIdentityEvidence ? 'Verify and save identity evidence' : 'Save verified HR evidence'}</button>
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
  const expectedConfirmation = bulkDeletionConfirmation(users.length, device.device_id)
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (reason.trim().length < 10) return setError('Record a reason of at least 10 characters.')
    if (confirmation !== expectedConfirmation) {
      return setError(`Type ${expectedConfirmation} exactly to continue.`)
    }
    if (!password) return setError('Password confirmation is required.')
    setBusy(true)
    try {
      const validation = await onRevalidate()
      if (validation.changed) {
        setError(validation.users.length
          ? 'The selected users changed while this confirmation was open. Review the refreshed selection and type the updated confirmation before retrying.'
          : 'None of the selected users remain eligible for deletion. Nothing was deleted.')
        return
      }
      const response = await api<{ job: UserDeletionJob }>(
        `/api/v2/devices/${device.connector_id}/user-deletion-jobs`,
        {
          method: 'POST',
          body: JSON.stringify({
            targets: validation.users.map((user) => ({
              user_key: user.user_key,
              expected_version: user.row_version,
            })),
            reason: reason.trim(),
            typed_confirmation: confirmation,
            password,
            idempotency_key: idempotency('bulk-delete-users'),
          }),
        },
      )
      onCreated(response.job)
      onClose()
    } catch (reasonValue) {
      setError(
        reasonValue instanceof Error
          ? reasonValue.message
          : 'The bulk deletion job could not be created.',
      )
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog
      titleId="bulk-user-deletion-title"
      title={`Delete ${users.length} terminal users`}
      description="ADD will process one user at a time and stop advancing if terminal verification is unsafe."
      onClose={onClose}
      className="bulk-deletion-dialog"
    >
      <form className="dialog-body" onSubmit={submit}>
        <div className="destructive-copy pattern-blocked">
          <Icon name="trash" />
          <div>
            <h3>{device.display_name}</h3>
            <p>{users.map((user) => `${user.display_name} (${user.user_id})`).join(' · ')}</p>
            <p>User records are removed from the ZKT. Attendance and ADD identity history remain preserved.</p>
          </div>
        </div>
        <label>Audit reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} rows={3} /></label>
        <label>Type “{expectedConfirmation}”<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label>
        <label>Confirm administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>Cancel</button>
          <button className="button destructive" disabled={busy}>{busy ? 'Creating durable job…' : `Delete ${users.length} users safely`}</button>
        </footer>
      </form>
    </Dialog>
  )
}

export function BulkDeletionProgress({
  job,
  onCancel,
}: {
  job: UserDeletionJob
  onCancel: (password: string) => Promise<void>
}) {
  const active = ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(job.status)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  return (
    <section className={`command-progress bulk-deletion-progress pattern-${statusPattern(job.status)}`} aria-live="polite">
      <span className="command-symbol"><Icon name={job.status === 'SUCCEEDED' ? 'check' : ['PARTIAL', 'FAILED', 'EXPIRED'].includes(job.status) ? 'alert' : 'refresh'} /></span>
      <div>
        <p className="eyebrow">DURABLE BULK USER DELETION</p>
        <h3>{job.status.replaceAll('_', ' ')}</h3>
        <p>
          {job.counts.succeeded} verified deleted · {job.counts.pending} pending ·{' '}
          {job.counts.failed} failed · {job.counts.canceled} canceled · {job.counts.expired} expired
        </p>
        {job.items.find((item) => item.error_message)?.error_message && (
          <small>{job.items.find((item) => item.error_message)?.error_message}</small>
        )}
      </div>
      {active && job.status !== 'CANCEL_REQUESTED' && (
        <div className="bulk-cancel">
          {!cancelOpen ? (
            <button className="button secondary" onClick={() => setCancelOpen(true)}>Cancel untouched users</button>
          ) : (
            <>
              <input aria-label="Administrator password to cancel" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Administrator password" />
              <button
                className="button destructive"
                disabled={busy || !password}
                onClick={async () => {
                  setBusy(true)
                  try {
                    await onCancel(password)
                  } finally {
                    setBusy(false)
                  }
                }}
              >
                {busy ? 'Canceling…' : 'Confirm cancel'}
              </button>
            </>
          )}
        </div>
      )}
    </section>
  )
}
