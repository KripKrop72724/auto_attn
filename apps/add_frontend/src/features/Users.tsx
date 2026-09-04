import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { api, ApiError, queryString } from '../api'
import {
  CommandProgress, Dialog, Metric, PageHeader, StatusBadge, buildMachinePreview,
  bulkDeletionConfirmation, confirmationMatches, dateTime, idempotency,
  identityConflictText, relativeTime, statusPattern, terminalCommandStates,
  useToast, utf8Length, validateUserDraft,
  type HistoricalIdentityDialogState, type IdentityResolutionDialogState,
  type UserDialogState,
} from '../App'
import { Icon } from '../Icon'
import type {
  Command, Device, DeviceUser, HistoricalIdentityCandidate, HistoricalIdentityReport,
  IdentityConflictGroup, IdentityConflictReport, IdentityIntegrity,
  UserCommandResponse, UserDeletionJob,
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
  const preview = cnic
    ? buildMachinePreview(displayName, cnic, shiftWorker)
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
    create: ['Add user to selected terminal', 'Creates a regular user on this ZKT only.'],
    edit: ['Edit device user', 'Name, replacement CNIC, shift status, and permanent role are verified after write.'],
    delete: ['Delete user from terminal', 'The user is removed; punches and identity history remain permanently preserved.'],
    lease: ['Grant enrollment access', 'The selected regular user becomes administrator for 10 minutes, then reverts automatically.'],
  } as const
  const [title, description] = copy[state.mode]
  return (
    <Dialog titleId="user-operation-title" title={title} description={description} onClose={onClose}>
      <form className="dialog-body" onSubmit={submit}>
        {(state.mode === 'create' || state.mode === 'edit') && (
          <>
            <div className="form-grid">
              <label>Full canonical name<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} maxLength={255} aria-invalid={Boolean(fieldErrors.display_name)} />{fieldErrors.display_name?.[0] && <small className="field-error">{fieldErrors.display_name[0]}</small>}</label>
              <label>{state.mode === 'edit' ? conflictRequiresCnic ? 'Replacement CNIC (required to resolve conflict)' : missingCnicRequiresCnic ? 'Replacement CNIC (required for missing CNIC)' : 'Replacement CNIC (leave blank to preserve)' : 'CNIC'}<input inputMode="numeric" autoComplete="off" value={cnic} onChange={(event) => setCnic(event.target.value.replace(/\D/g, '').slice(0, 13))} placeholder="13 digits" required={editRequiresCnic} aria-invalid={Boolean(fieldErrors.cnic)} />{fieldErrors.cnic?.[0] && <small className="field-error">{fieldErrors.cnic[0]}</small>}</label>
              {state.mode === 'create' && <label>Employee/user ID override (optional)<input inputMode="numeric" value={userIdOverride} onChange={(event) => setUserIdOverride(event.target.value.replace(/\D/g, '').slice(0, 24))} aria-invalid={Boolean(fieldErrors.user_id_override)} />{fieldErrors.user_id_override?.[0] && <small className="field-error">{fieldErrors.user_id_override[0]}</small>}</label>}
              {state.mode === 'edit' && <label>Terminal role<select value={privilege} onChange={(event) => { setPrivilege(Number(event.target.value) as 0 | 14); setConfirmation(''); setElevationReason('') }}><option value={0}>Regular user</option><option value={14}>Permanent administrator</option></select><small>Prefer the separate 10-minute enrollment lease for routine fingerprint enrollment.</small></label>}
            </div>
            <label className="check-field"><input type="checkbox" checked={shiftWorker} onChange={(event) => setShiftWorker(event.target.checked)} /><span><strong>Shift worker</strong><small>Adds the -S- identity marker used for raw-punch handling.</small></span></label>
            <div className="preview-box"><span>Exact ZKT 24-byte name preview</span><code>{preview}</code><small>{cnic ? `${utf8Length(preview)} / 24 UTF-8 bytes` : editRequiresCnic ? 'Verified CNIC is required before this update can be queued.' : 'Stored CNIC remains write-only.'}</small></div>
          </>
        )}
        {state.mode === 'delete' && (
          <div className="destructive-copy pattern-blocked">
            <Icon name="trash" />
            <div><h3>{state.user.display_name}</h3><p>UID {state.user.uid} · User ID {state.user.user_id}</p><p>ADD and ZKT attendance records will not be deleted.</p></div>
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
}: {
  state: Exclude<HistoricalIdentityDialogState, null>
  device: Device
  onClose: () => void
  onComplete: () => Promise<void>
  toast: ReturnType<typeof useToast>
}) {
  const candidate = state.candidate
  const [cnic, setCnic] = useState('')
  const [employeeId, setEmployeeId] = useState('')
  const [serviceNumber, setServiceNumber] = useState(candidate.user_id)
  const [employeeName, setEmployeeName] = useState(candidate.display_name)
  const [zoneCode, setZoneCode] = useState(device.zone_id)
  const [reason, setReason] = useState('')
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
    if (confirmation !== expectedConfirmation) {
      return setError(`Type ${expectedConfirmation} exactly to continue.`)
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
      const response = await api<{ repaired_events: number }>(
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
            typed_confirmation: confirmation,
            password,
            idempotency_key: idempotency(
              currentIdentityEvidence
                ? 'historical-current-identity'
                : 'historical-directory-identity',
            ),
          }),
        },
      )
      await onComplete()
      toast.notice(
        `${response.repaired_events.toLocaleString()} preserved attendance event${response.repaired_events === 1 ? '' : 's'} requeued for Oracle confirmation.`,
      )
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
      title={currentIdentityEvidence ? 'Verify preserved cohort against current identity' : 'Enter verified HR identity evidence'}
      description={currentIdentityEvidence
        ? 'Use the authoritative Oracle capture identity to confirm this exact historical cohort belongs to the unchanged current terminal user.'
        : 'Use authoritative HR directory evidence only. ADD will preserve every attendance event and requeue only an exact, unambiguous match.'}
      onClose={onClose}
    >
      <form className="dialog-body" onSubmit={submit}>
        <div className="info-copy pattern-waiting">
          <Icon name="shield" />
          <div>
            <h3>{candidate.display_name}</h3>
            <p>User {candidate.user_id} · UID {candidate.uid} · version {candidate.row_version}</p>
            <p>{candidate.event_count.toLocaleString()} preserved events from {dateTime(candidate.first_event_at)} to {dateTime(candidate.last_event_at)}.</p>
          </div>
        </div>
        <div className="form-grid">
          <label>Authoritative CNIC<input inputMode="numeric" autoComplete="off" value={cnic} onChange={(event) => setCnic(event.target.value.replace(/\D/g, '').slice(0, 13))} placeholder="13 digits" /></label>
          {!currentIdentityEvidence && <label>HR employee ID<input inputMode="numeric" value={employeeId} onChange={(event) => setEmployeeId(event.target.value.replace(/\D/g, '').slice(0, 32))} /></label>}
          {!currentIdentityEvidence && <label>HR service number<input value={serviceNumber} onChange={(event) => setServiceNumber(event.target.value.replace(/[^A-Za-z0-9._-]/g, '').slice(0, 64))} /></label>}
          <label>{currentIdentityEvidence ? 'Authoritative employee name' : 'HR employee name'}<input value={employeeName} onChange={(event) => setEmployeeName(event.target.value)} maxLength={255} /></label>
          {!currentIdentityEvidence && <label>HR zone code<input value={zoneCode} onChange={(event) => setZoneCode(event.target.value)} maxLength={64} /></label>}
        </div>
        <div className="message pattern-blocked" role="note">
          <Icon name="alert" />
          {currentIdentityEvidence
            ? 'Do not infer or guess a CNIC. ADD will require it to match the encrypted current identity, the exact terminal user ID, stable historical name, reviewed cohort token, and current row version.'
            : 'Do not infer or guess a CNIC. The terminal service number and employee name must match the authoritative HR record.'}
        </div>
        <label>Audit reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} rows={3} /></label>
        <label>Type “{expectedConfirmation}”<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label>
        <label>Confirm administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        <footer className="dialog-actions">
          <button className="button secondary" type="button" onClick={onClose}>Cancel</button>
          <button className="button primary" disabled={busy}>{busy ? 'Verifying and requeuing…' : currentIdentityEvidence ? 'Verify current identity and requeue' : 'Save verified HR evidence'}</button>
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

export function UsersView({
  devices,
  selectedDeviceId,
  onSelectDevice,
  revision,
  toast,
  refreshFleet,
}: {
  devices: Device[]
  selectedDeviceId: string
  onSelectDevice: (id: string) => void
  revision: number
  toast: ReturnType<typeof useToast>
  refreshFleet: () => Promise<void>
}) {
  const selected = devices.find((device) => device.connector_id === selectedDeviceId)
  const [rows, setRows] = useState<DeviceUser[]>([])
  const [nextCursor, setNextCursor] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [query, setQuery] = useState('')
  const [identity, setIdentity] = useState('ALL')
  const [role, setRole] = useState('ALL')
  const [dialog, setDialog] = useState<UserDialogState>(null)
  const [command, setCommand] = useState<Command | null>(null)
  const [integrity, setIntegrity] = useState<IdentityIntegrity | null>(null)
  const [conflictReport, setConflictReport] = useState<IdentityConflictReport | null>(null)
  const [resolutionDialog, setResolutionDialog] = useState<IdentityResolutionDialogState>(null)
  const [historicalReport, setHistoricalReport] = useState<HistoricalIdentityReport | null>(null)
  const [historicalDialog, setHistoricalDialog] = useState<HistoricalIdentityDialogState>(null)
  const [selectedUserKeys, setSelectedUserKeys] = useState<Set<string>>(new Set())
  const [bulkDialogOpen, setBulkDialogOpen] = useState(false)
  const [deletionJob, setDeletionJob] = useState<UserDeletionJob | null>(null)
  const userTableRef = useRef<HTMLDivElement>(null)
  const userVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => userTableRef.current,
    estimateSize: () => 86,
    overscan: 10,
    enabled: rows.length > 200,
  })

  const load = useCallback(async (cursor?: number, append = false) => {
    if (!selected) {
      setRows([])
      setIntegrity(null)
      setConflictReport(null)
      setHistoricalReport(null)
      setNextCursor(null)
      return
    }
    setLoading(true)
    try {
      const compact = query.replace(/\D/g, '')
      const cnicSearch = compact.length === 13 && compact === query.replace(/[\s-]/g, '')
      const directoryPath = `/api/v2/devices/${selected.connector_id}/users${queryString({
        q: cnicSearch ? undefined : query,
        cnic: cnicSearch ? compact : undefined,
        identity: identity === 'ALL' ? undefined : identity,
        privilege: role === 'ALL' ? undefined : role,
        cursor,
        limit: 200,
      })}`
      if (append && cursor) {
        const result = await api<{ rows: DeviceUser[]; next_cursor: number | null }>(directoryPath)
        setRows((current) => [...current, ...result.rows])
        setNextCursor(result.next_cursor)
        return
      }
      const [result, conflicts, history, latestJob] = await Promise.all([
        api<{
          rows: DeviceUser[]
          identity_integrity: IdentityIntegrity
          next_cursor: number | null
        }>(directoryPath),
        api<IdentityConflictReport>(
          `/api/v2/devices/${selected.connector_id}/identity-conflicts`,
        ),
        api<HistoricalIdentityReport>(
          `/api/v2/devices/${selected.connector_id}/historical-identities`,
        ),
        api<{ job: UserDeletionJob | null }>(
          `/api/v2/devices/${selected.connector_id}/user-deletion-jobs/latest`,
        ),
      ])
      setRows(result.rows)
      setNextCursor(result.next_cursor)
      setSelectedUserKeys((current) => {
        const available = new Set(result.rows.map((user) => user.user_key))
        return new Set([...current].filter((key) => available.has(key)))
      })
      setIntegrity(result.identity_integrity || null)
      setConflictReport(conflicts)
      setHistoricalReport(history?.totals ? history : null)
      setDeletionJob(latestJob.job)
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : 'Unable to load users.')
    } finally {
      setLoading(false)
    }
  }, [identity, query, role, selected, toast.error])

  useEffect(() => {
    const timeout = window.setTimeout(() => void load(), 250)
    return () => window.clearTimeout(timeout)
  }, [load, revision])

  useEffect(() => {
    setSelectedUserKeys(new Set())
    setBulkDialogOpen(false)
    setHistoricalDialog(null)
  }, [selectedDeviceId])

  useEffect(() => {
    if (!deletionJob || !['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(deletionJob.status)) {
      return
    }
    const timeout = window.setTimeout(async () => {
      try {
        const response = await api<{ job: UserDeletionJob }>(
          `/api/v2/user-deletion-jobs/${deletionJob.job_id}`,
        )
        const finished = !['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(response.job.status)
        setDeletionJob(response.job)
        if (finished) {
          setSelectedUserKeys(new Set())
          await Promise.all([load(), refreshFleet()])
          if (response.job.status === 'SUCCEEDED') {
            toast.notice('Every selected user was deleted and verified; attendance was preserved.')
          } else {
            toast.error(`Bulk deletion ended as ${response.job.status}. Review the per-user result.`)
          }
        }
      } catch (reason) {
        toast.error(reason instanceof Error ? reason.message : 'Unable to refresh deletion progress.')
      }
    }, 1600)
    return () => window.clearTimeout(timeout)
  }, [deletionJob, load, refreshFleet, toast.error, toast.notice])

  useEffect(() => {
    if (!command || terminalCommandStates.has(command.status)) return
    const timeout = window.setTimeout(async () => {
      try {
        const updated = await api<Command>(`/api/v2/commands/${command.command_id}`)
        setCommand(updated)
        if (terminalCommandStates.has(updated.status)) {
          await Promise.all([load(), refreshFleet()])
          if (updated.status === 'SUCCEEDED') toast.notice(`${updated.type.replaceAll('_', ' ')} completed and was verified.`)
          else toast.error(updated.error_message || `${updated.type.replaceAll('_', ' ')} ended as ${updated.status}.`)
        }
      } catch (reason) {
        toast.error(reason instanceof Error ? reason.message : 'Unable to refresh command state.')
      }
    }, 1600)
    return () => window.clearTimeout(timeout)
  }, [command, load, refreshFleet, toast.error, toast.notice])

  const cancel = async (row: Command) => {
    try {
      setCommand(await api<Command>(`/api/v2/commands/${row.command_id}/cancel`, { method: 'POST', body: '{}' }))
      await load()
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : 'Unable to cancel command.')
    }
  }

  const writable = Boolean(
    selected?.zkt?.certification_state === 'CERTIFIED' &&
      selected.zkt.snapshot_complete &&
      selected.zkt.capabilities.user_write,
  )
  const deleteWritable = Boolean(writable && selected?.zkt?.capabilities.delete_user)
  const eligibleRows = rows.filter(
    (user) =>
      user.privilege !== 14 &&
      !user.read_only &&
      !user.current_command_state,
  )
  const selectedUsers = eligibleRows.filter((user) => selectedUserKeys.has(user.user_key))
  const activeDeletionJob = Boolean(
    deletionJob && ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(deletionJob.status),
  )
  const toggleUser = (userKey: string, checked: boolean) => {
    setSelectedUserKeys((current) => {
      const next = new Set(current)
      if (checked) next.add(userKey)
      else next.delete(userKey)
      return next
    })
  }
  const cancelDeletionJob = async (password: string) => {
    if (!deletionJob) return
    try {
      const response = await api<{ job: UserDeletionJob }>(
        `/api/v2/user-deletion-jobs/${deletionJob.job_id}/cancel`,
        { method: 'POST', body: JSON.stringify({ password }) },
      )
      setDeletionJob(response.job)
      toast.notice('Cancellation recorded. Any running user will finish verification; untouched users will be skipped.')
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : 'Unable to cancel deletion job.')
      throw reason
    }
  }

  const renderUserRow = (user: DeviceUser, style?: CSSProperties, virtualIndex?: number) => (
    <article ref={virtualIndex == null ? undefined : userVirtualizer.measureElement} data-index={virtualIndex} style={style} key={user.user_key} className={`user-row ${user.identity_conflict_resolved ? 'identity-resolved' : user.identity_conflict_code ? 'identity-conflict' : user.identity_complete ? '' : 'identity-missing'}`}>
      <div className="user-person">
        <input className="user-select" type="checkbox" aria-label={`Select ${user.display_name} for bulk deletion`} checked={selectedUserKeys.has(user.user_key)} disabled={!deleteWritable || activeDeletionJob || user.privilege === 14 || user.read_only || Boolean(user.current_command_state)} onChange={(event) => toggleUser(user.user_key, event.target.checked)} />
        <span className="avatar">{user.display_name.slice(0, 2).toUpperCase()}</span><span><strong>{user.display_name}</strong><small>{user.identity_conflict_code ? identityConflictText(user) : user.cnic_masked || 'CNIC missing · punches blocked until enriched'}</small></span>
      </div>
      <div><strong>User {user.user_id}</strong><small>UID {user.uid} · v{user.row_version}</small><code>{user.machine_name_preview || 'No machine preview'}</code></div>
      <div><StatusBadge state={user.privilege === 14 ? 'ADMINISTRATOR' : 'REGULAR'} /><small>{user.shift_worker ? 'Shift worker' : 'Standard worker'}</small></div>
      <div><strong>{relativeTime(user.observed_at)}</strong><small>{dateTime(user.observed_at)}</small></div>
      <div className="row-actions">
        <button className="icon-button" disabled={!writable} onClick={() => setDialog({ mode: 'edit', user })} aria-label={`Edit ${user.display_name}`}><Icon name="edit" /></button>
        <button className="icon-button" disabled={!writable || user.privilege !== 0} onClick={() => setDialog({ mode: 'lease', user })} aria-label={`Grant enrollment access to ${user.display_name}`}><Icon name="shield" /></button>
        <button className="icon-button" disabled={!writable || user.privilege === 14} onClick={() => setDialog({ mode: 'delete', user })} aria-label={`Delete ${user.display_name}`}><Icon name="trash" /></button>
      </div>
    </article>
  )

  return (
    <>
      <PageHeader
        eyebrow="SELECTED-TERMINAL USER CONTROL"
        title="Device users"
        description="Create, enrich, edit, elevate, and safely remove identities on one selected ZKT terminal."
        action={selected && <button className="button primary" disabled={!writable} onClick={() => setDialog({ mode: 'create' })}><Icon name="userPlus" /> Add user</button>}
      />
      <section className="panel selection-panel">
        <label>Selected terminal<select value={selectedDeviceId} onChange={(event) => onSelectDevice(event.target.value)}><option value="">Choose a terminal</option>{devices.map((device) => <option key={device.connector_id} value={device.connector_id}>{device.display_name} · {device.zkt?.serial || 'awaiting serial'}</option>)}</select></label>
        {selected && <div className="selected-device-summary"><StatusBadge state={selected.state} live={selected.connected} /><span>{selected.zkt?.model || 'ZKT terminal'} · {selected.zkt?.ip_address || 'No IP'}</span><span>Users {selected.zkt?.user_count ?? '—'} · Last sync {relativeTime(selected.last_seen_at)}</span></div>}
      </section>
      {!selected ? (
        <section className="panel empty-state"><Icon name="users" /><h2>Select a terminal to manage its users.</h2><p>Mutations are always scoped to one selected ZKT device.</p></section>
      ) : (
        <>
          {!writable && <div className="capability-banner pattern-waiting"><Icon name="shield" /><div><strong>User writes are unavailable.</strong><span>{selected.zkt?.writes_disabled_reason || 'The terminal is not yet write-certified or its complete snapshot is pending.'}</span></div><StatusBadge state={selected.zkt?.certification_state || 'READ_ONLY'} /></div>}
          {integrity && integrity.unresolved_duplicate_users > 0 && (
            <div className="capability-banner pattern-blocked" role="status">
              <Icon name="alert" />
              <div>
                <strong>Exact CNIC conflicts are present in the terminal data.</strong>
                <span>
                  The latest {integrity.source === 'CURRENT_COMPLETE_ZKT_SNAPSHOT' ? 'complete ' : ''}
                  ZKT snapshot reports {integrity.unresolved_duplicate_users} unresolved users across{' '}
                  {integrity.unresolved_duplicate_groups} exact-CNIC groups. Matching terminal user IDs are
                  quarantined until corrected or explicitly verified as the same employee.
                </span>
              </div>
              <StatusBadge state="CORRECTION REQUIRED" />
            </div>
          )}
          {command && <CommandProgress command={command} onCancel={cancel} />}
          {deletionJob && <BulkDeletionProgress job={deletionJob} onCancel={cancelDeletionJob} />}
          {historicalReport && historicalReport.totals.unresolved_events > 0 && (
            <details className="panel conflict-workbench disclosure-panel" aria-label="Historical identity backlog">
              <summary><span><Icon name="shield" /><strong>Historical identity backlog</strong></span><span>{historicalReport.totals.unresolved_events.toLocaleString()} preserved events <Icon name="chevron" /></span></summary>
              <div className="section-heading">
                <div>
                  <p className="eyebrow">PRESERVED ATTENDANCE · IDENTITY REQUIRED</p>
                  <h2>Historical identity backlog</h2>
                  <p>
                    {historicalReport.totals.unresolved_events.toLocaleString()} events remain fail-closed:
                    {' '}{historicalReport.totals.blocked_identity.toLocaleString()} missing verified identity and
                    {' '}{historicalReport.totals.quarantined_identity_reuse.toLocaleString()} quarantined for identity reuse.
                    {' '}{historicalReport.totals.unassigned_events.toLocaleString()} are not linked to one deleted terminal user;
                    {' '}{(historicalReport.totals.actionable_event_groups ?? 0).toLocaleString()} exact cohorts can accept guarded HR evidence.
                  </p>
                </div>
                <StatusBadge state="HR EVIDENCE REQUIRED" />
              </div>
              <div className="conflict-groups">
                {[...historicalReport.rows, ...(historicalReport.unassigned_groups ?? [])].map((candidate) => (
                  <article key={candidate.source_user_key || candidate.group_token} className="conflict-group pattern-blocked">
                    <div className="conflict-group-heading">
                      <div>
                        <strong>{candidate.display_name}</strong>
                        <small>
                          User {candidate.user_id} · UID {candidate.uid || 'missing'}
                          {candidate.row_version == null ? ' · exact event cohort' : ` · version ${candidate.row_version}`}
                        </small>
                      </div>
                      <StatusBadge state={candidate.resolution_path.replaceAll('_', ' ')} />
                    </div>
                    <div className="conflict-members">
                      <div>
                        <span>
                          <strong>{candidate.event_count.toLocaleString()} preserved events</strong>
                          <small>{candidate.blocked_count.toLocaleString()} blocked · {candidate.quarantined_count.toLocaleString()} quarantined</small>
                        </span>
                        <span>
                          <strong>{dateTime(candidate.first_event_at)}</strong>
                          <small>through {dateTime(candidate.last_event_at)}</small>
                        </span>
                      </div>
                    </div>
                    <div className="conflict-group-actions">
                      <small>
                        {candidate.operator_actionable
                          ? candidate.resolution_path === 'ACTIVE_USER_ENRICHMENT'
                            ? 'This preserved cohort is linked to one current user. Enrich that certified terminal record with authoritative CNIC evidence.'
                            : candidate.resolution_path === 'CURRENT_IDENTITY_EVIDENCE'
                              ? 'This exact legacy cohort can be compared with the unchanged, CNIC-complete current user. Authoritative CNIC and name evidence are still required.'
                            : 'Requires exact authoritative HR CNIC, employee ID, service number, name, and audit approval.'
                          : candidate.source_kind === 'EVENT_GROUP'
                            ? 'This cohort remains fail-closed because it lacks a unique UID, has conflicting terminal names, or is linked to another identity.'
                            : 'This row remains fail-closed until its identity conflict or reuse review is resolved.'}
                      </small>
                      <button
                        className="button secondary"
                        disabled={!candidate.operator_actionable}
                        onClick={() => {
                          if (candidate.resolution_path === 'ACTIVE_USER_ENRICHMENT') {
                            const activeUser = rows.find(
                              (row) => row.user_key === candidate.active_user_key,
                            )
                            if (activeUser) setDialog({ mode: 'edit', user: activeUser })
                            else toast.error('Linked current user is no longer available. Refresh and retry.')
                          } else {
                            setHistoricalDialog({ candidate })
                          }
                        }}
                      >
                        <Icon name="shield" />
                        {candidate.resolution_path === 'ACTIVE_USER_ENRICHMENT'
                          ? 'Enrich current user'
                          : candidate.resolution_path === 'CURRENT_IDENTITY_EVIDENCE'
                            ? 'Verify against current identity'
                            : 'Enter verified HR evidence'}
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            </details>
          )}
          {conflictReport && conflictReport.raw_duplicate_groups > 0 && (
            <details className="panel conflict-workbench disclosure-panel" aria-label="Identity conflict review">
              <summary><span><Icon name="alert" /><strong>Exact-CNIC identity review</strong></span><span>{conflictReport.unresolved_groups} awaiting review <Icon name="chevron" /></span></summary>
              <div className="section-heading">
                <div>
                  <p className="eyebrow">REVERSIBLE IDENTITY REVIEW</p>
                  <h2>Exact-CNIC terminal groups</h2>
                  <p>
                    {conflictReport.resolved_groups} verified · {conflictReport.unresolved_groups} awaiting review.
                    ADD has {conflictReport.evidence_scope.add_attendance_count.toLocaleString()} of the terminal’s{' '}
                    {conflictReport.evidence_scope.terminal_attendance_count.toLocaleString()} punches
                    {conflictReport.evidence_scope.attendance_coverage_percent == null
                      ? ''
                      : ` (${conflictReport.evidence_scope.attendance_coverage_percent}% evidence coverage)`}.
                  </p>
                </div>
                <StatusBadge state={conflictReport.unresolved_groups ? 'REVIEW REQUIRED' : 'RESOLVED'} />
              </div>
              <div className="conflict-groups">
                {conflictReport.groups.map((group) => (
                  <article key={group.group_token} className={`conflict-group pattern-${group.status === 'UNRESOLVED' ? 'blocked' : 'confirmed'}`}>
                    <div className="conflict-group-heading">
                      <div><strong>{group.cnic_masked || 'Masked CNIC'}</strong><small>{group.classification.replaceAll('_', ' ')}</small></div>
                      <StatusBadge state={group.status} />
                    </div>
                    <div className="conflict-members">
                      {group.members.map((member) => (
                        <div key={member.user_key}>
                          <span><strong>{member.display_name}</strong><small>User {member.user_id} · UID {member.uid}</small></span>
                          <span><strong>{member.punch_evidence.captured_count.toLocaleString()} captured</strong><small>{member.punch_evidence.last_captured_at ? `Last ${dateTime(member.punch_evidence.last_captured_at)}` : 'No punches in ADD evidence window'}</small></span>
                        </div>
                      ))}
                    </div>
                    <div className="conflict-group-actions">
                      <small>{group.status === 'UNRESOLVED' ? group.recommended_action.replaceAll('_', ' ') : group.resolution_reason}</small>
                      <button
                        className={`button ${group.status === 'UNRESOLVED' ? 'secondary' : 'text-button'}`}
                        onClick={() => setResolutionDialog({ mode: group.status === 'UNRESOLVED' ? 'resolve' : 'revoke', group })}
                      >
                        <Icon name={group.status === 'UNRESOLVED' ? 'shield' : 'alert'} />
                        {group.status === 'UNRESOLVED' ? 'Review same-employee alias' : 'Revoke resolution'}
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            </details>
          )}
          <section className="panel">
            <div className="toolbar user-toolbar">
              <label className="search-field"><span className="sr-only">Search users</span><Icon name="search" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search name, user ID, or exact CNIC" /></label>
              <label><span className="sr-only">Identity completeness</span><select value={identity} onChange={(event) => setIdentity(event.target.value)}><option value="ALL">All identities</option><option value="COMPLETE">CNIC complete</option><option value="MISSING">CNIC missing or unresolved</option><option value="CONFLICT">CNIC conflict</option><option value="RESOLVED_ALIAS">Verified aliases</option></select></label>
              <label><span className="sr-only">Role</span><select value={role} onChange={(event) => setRole(event.target.value)}><option value="ALL">All roles</option><option value="0">Regular users</option><option value="14">Administrators</option></select></label>
              <button className="button secondary" onClick={() => void load()}><Icon name="refresh" /> Refresh users</button>
              <label className="check-field bulk-select-all">
                <input
                  type="checkbox"
                  checked={eligibleRows.length > 0 && selectedUsers.length === eligibleRows.length}
                  disabled={!deleteWritable || activeDeletionJob || !eligibleRows.length}
                  onChange={(event) =>
                    setSelectedUserKeys(
                      event.target.checked
                        ? new Set(eligibleRows.map((user) => user.user_key))
                        : new Set(),
                    )
                  }
                />
                <span><strong>Select eligible on loaded rows</strong><small>{selectedUsers.length} selected of {eligibleRows.length} eligible currently loaded</small></span>
              </label>
              <button
                className="button destructive"
                disabled={!deleteWritable || activeDeletionJob || !selectedUsers.length}
                onClick={() => setBulkDialogOpen(true)}
              >
                <Icon name="trash" /> Delete selected ({selectedUsers.length})
              </button>
            </div>
            <div ref={userTableRef} className={`user-table ${rows.length > 200 ? 'user-table-virtualized' : ''}`} aria-busy={loading}>
              <div className="user-table-head"><span>Identity</span><span>Terminal record</span><span>Role & shift</span><span>Last sync</span><span>Actions</span></div>
              {loading && <div className="empty-state compact"><Icon name="refresh" /><p>Reading the selected terminal user view…</p></div>}
              {!loading && rows.length <= 200 && rows.map((user) => renderUserRow(user))}
              {!loading && rows.length > 200 && <div className="virtual-user-rows" style={{ height: userVirtualizer.getTotalSize() }}>{userVirtualizer.getVirtualItems().map((item) => renderUserRow(rows[item.index], { position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${item.start}px)` }, item.index))}</div>}
              {!loading && !rows.length && <div className="empty-state"><Icon name="users" /><h3>No users match this selected-terminal view.</h3><p>Change the filters or add the first ADD-managed user.</p></div>}
            </div>
            {nextCursor && <div className="load-more"><button className="button secondary" disabled={loading} onClick={() => void load(nextCursor, true)}>{loading ? 'Loading…' : 'Load more terminal users'}</button><small>{rows.length.toLocaleString()} users loaded · bulk selection applies only to loaded rows</small></div>}
          </section>
        </>
      )}
      {dialog && selected && <UserOperationDialog state={dialog} device={selected} onClose={() => setDialog(null)} onCommand={setCommand} toast={toast} />}
      {bulkDialogOpen && selected && selectedUsers.length > 0 && (
        <BulkDeletionDialog
          users={selectedUsers}
          device={selected}
          onRevalidate={async () => {
            const response = await api<{ rows: DeviceUser[] }>(
              `/api/v2/devices/${selected.connector_id}/users/validate-selection`,
              {
                method: 'POST',
                body: JSON.stringify({ user_keys: selectedUsers.map((user) => user.user_key) }),
              },
            )
            const freshByKey = new Map(response.rows.map((user) => [user.user_key, user]))
            const validated = selectedUsers
              .map((user) => freshByKey.get(user.user_key))
              .filter((user): user is DeviceUser => Boolean(
                user && user.privilege !== 14 && !user.read_only && !user.current_command_state,
              ))
            const changed = validated.length !== selectedUsers.length
              || selectedUsers.some((user, index) => validated[index]?.row_version !== user.row_version)
            setSelectedUserKeys(new Set(validated.map((user) => user.user_key)))
            return { users: validated, changed }
          }}
          onClose={() => setBulkDialogOpen(false)}
          onCreated={(job) => {
            setDeletionJob(job)
            toast.notice('Durable bulk deletion job created. ADD will verify one user at a time.')
          }}
        />
      )}
      {resolutionDialog && selected && <IdentityResolutionDialog state={resolutionDialog} device={selected} onClose={() => setResolutionDialog(null)} onComplete={(report) => { setConflictReport(report); void load() }} toast={toast} />}
      {historicalDialog && selected && (
        <HistoricalIdentityResolutionDialog
          state={historicalDialog}
          device={selected}
          onClose={() => setHistoricalDialog(null)}
          onComplete={async () => {
            await Promise.all([load(), refreshFleet()])
          }}
          toast={toast}
        />
      )}
    </>
  )
}
