import { useMemo, useRef, useState, type FormEvent } from 'react'
import { api, ApiError } from '../api'
import { Dialog, StatusBadge, dateTime, idempotency, type useToast } from '../App'
import { Icon } from '../Icon'
import { humanizeStatus, normalizedStatus } from '../status'
import type { CleanupApply, CleanupConnectorEffect, CleanupPreview, CleanupPreviewRow, Device } from '../types'
import { OperatorFailureMessage, explainOperatorError, type OperatorFailure } from './AlertResolveDialog'
import './DeviceHealth.css'

type Toast = Pick<ReturnType<typeof useToast>, 'notice' | 'error'>
type Scope = 'SUGGESTED' | 'CHOSEN' | 'ALL'

// storage_recovery.TARGETS (Peshawar 02 and 06). Each preview returns the
// server's suggested_first_scope, which replaces this seed.
export const PESHAWAR_FIRST_SCOPE = [
  'bf4badc7-5f9c-42aa-8b3a-8a43f8daeb5e',
  '233dac02-eb1b-4598-a876-e3a7b1ecfd54',
]
const MAX_SCOPE = 50
const MAX_ALERTS = 200
const MAX_ERROR_FIXES = 50

export const cleanupConfirmation = (alerts: number, devices: number) => `RESOLVE ${alerts} ALERTS ON ${devices} DEVICES`

const classLabels: Record<string, string> = {
  STRANDED_DIAGNOSTICS: 'Stranded diagnostics',
  CONDITION_CLEARED: 'Condition cleared',
  SUPERSEDED_ACK: 'Superseded acknowledgement',
  STALE_ERROR_CODE: 'Stale device error',
  REVIEW: 'Needs review',
}

const applyRefusals: Record<string, string> = {
  CLEANUP_DISABLED: 'Applying a cleanup is switched off on this server (ADD_DEVICE_HEALTH_CLEANUP_APPLY_ENABLED is not enabled). Nothing was changed. The preview stays available and read-only; the owner enables apply only for the cleanup window.',
  PREVIEW_INVALID: 'The preview signature did not verify: it belongs to another administrator or was altered. Nothing was changed. Run the preview again.',
  PREVIEW_EXPIRED: 'This preview expired; previews are valid for 15 minutes. Nothing was changed. Run the preview again.',
  SCOPE_CHANGED: 'Device evidence changed after the preview, for example a heartbeat raised or resolved an alert. Nothing was changed. Run the preview again and review the new plan.',
  SELECTION_OUT_OF_SCOPE: 'A selected alert or device is not part of this preview. Nothing was changed. Run the preview again.',
  CONFIRMATION_MISMATCH: 'The typed confirmation does not match the selection. Nothing was changed. Type the phrase exactly as shown.',
  IDEMPOTENCY_KEY_REUSED: 'This request key was already used for a different cleanup. A new key is ready; apply again.',
}
const rerunCodes = new Set(['PREVIEW_INVALID', 'PREVIEW_EXPIRED', 'SCOPE_CHANGED', 'SELECTION_OUT_OF_SCOPE'])

const isReview = (row: CleanupPreviewRow) => normalizedStatus(row.class).toUpperCase() === 'REVIEW' || row.review === true
const isDurability = (row: CleanupPreviewRow) =>
  row.code === 'ESP_DURABILITY_FAULT' || row.residual_code === 'ESP_PRESERVATION_UNVERIFIED'
// Durability and review rows are opt-in whatever the server preselects.
const preselected = (row: CleanupPreviewRow) => row.default_selected && !isDurability(row) && !isReview(row)
const alertRows = (preview: CleanupPreview) =>
  preview.rows.filter((row): row is CleanupPreviewRow & { alert_id: number } => typeof row.alert_id === 'number')
// Class D is per device: SET_LAST_ERROR_CODE to the derived value, preselected.
const errorFixCandidates = (preview: CleanupPreview) => Array.from(new Set([
  ...preview.connectors.filter((connector) => connector.error_fix).map((connector) => connector.connector_id),
  ...preview.rows
    .filter((row) => row.alert_id == null && normalizedStatus(row.class).toUpperCase() === 'STALE_ERROR_CODE')
    .map((row) => row.connector_id),
]))
const scopeIds = (preview: CleanupPreview) => (preview.suggested_first_scope || [])
  .map((target) => typeof target === 'string' ? target : target?.connector_id)
  .filter((id): id is string => typeof id === 'string' && Boolean(id))

const sameSet = <T,>(left: Set<T>, right: Set<T>) => left.size === right.size && [...left].every((item) => right.has(item))

function firmwareBoot(firmware?: string | null, boot?: string | null) {
  const parts = [firmware ? `Firmware ${firmware.replace(/^zone-lite-/i, '')}` : null, boot ? `boot ${boot.slice(0, 8)}` : null].filter(Boolean)
  return parts.length ? parts.join(' · ') : 'Not recorded'
}

// Keys and machine tokens read as words; free text stays as written.
const label = (key: string) => humanizeStatus(key.toUpperCase())
const token = (value: string) => /^[A-Z0-9_]+$/.test(value) ? humanizeStatus(value).toLowerCase() : value

function evidenceText(value: CleanupPreviewRow['evidence']) {
  if (!value) return ''
  if (typeof value === 'string') return value.slice(0, 300)
  if (typeof value.summary === 'string') return value.summary.slice(0, 300)
  return Object.entries(value)
    .filter(([, item]) => ['string', 'number', 'boolean'].includes(typeof item))
    .slice(0, 8)
    .map(([key, item]) => `${label(key)} ${String(item)}`)
    .join(' · ')
    .slice(0, 300)
}

function holdEffectLines(value: CleanupConnectorEffect['hold_effects']): string[] {
  if (!value) return []
  const describe = (entry: Record<string, unknown>) => Object.entries(entry)
    .map(([key, item]) => `${label(key)}: ${typeof item === 'string' ? token(item) : String(item)}`)
    .join(' · ')
  if (Array.isArray(value)) return value.map((item) => typeof item === 'string' ? item : describe(item)).filter(Boolean)
  return [describe(value)].filter(Boolean)
}

const errorLabel = (code?: string | null) => code ? humanizeStatus(code) : 'none'

export function HealthCleanupDialog({
  devices,
  toast,
  onClose,
  onApplied,
}: {
  devices: Device[]
  toast: Toast
  onClose: () => void
  onApplied?: () => Promise<void> | void
}) {
  const [scope, setScope] = useState<Scope>('SUGGESTED')
  const [suggested, setSuggested] = useState<string[]>(PESHAWAR_FIRST_SCOPE)
  const [chosen, setChosen] = useState<string[]>([])
  const [search, setSearch] = useState('')
  const [preview, setPreview] = useState<CleanupPreview | null>(null)
  const [previewScope, setPreviewScope] = useState<string[] | null>(null)
  const [selectedAlerts, setSelectedAlerts] = useState<Set<number>>(new Set())
  const [selectedFixes, setSelectedFixes] = useState<Set<string>>(new Set())
  const [reason, setReason] = useState('')
  const [typed, setTyped] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState<'preview' | 'apply' | null>(null)
  const [failure, setFailure] = useState<(OperatorFailure & { stage: 'preview' | 'apply' }) | null>(null)
  const [result, setResult] = useState<CleanupApply | null>(null)
  const key = useRef(idempotency('device-health-cleanup'))
  const names = useMemo(() => new Map(devices.map((device) => [device.connector_id, device.display_name])), [devices])
  const deviceName = (connectorId: string, effect?: CleanupConnectorEffect) =>
    effect?.display_name || names.get(connectorId) || connectorId

  const runPreview = async () => {
    const connectorIds = scope === 'ALL' ? null : scope === 'SUGGESTED' ? suggested : chosen
    setBusy('preview')
    setFailure(null)
    setResult(null)
    try {
      const value = await api<CleanupPreview>('/api/v1/device-health/cleanup/preview', {
        method: 'POST',
        body: JSON.stringify({ connector_ids: connectorIds }),
      })
      setPreview(value)
      setPreviewScope(connectorIds)
      setSelectedAlerts(new Set(alertRows(value).filter(preselected).map((row) => row.alert_id)))
      setSelectedFixes(new Set(errorFixCandidates(value)))
      setTyped('')
      key.current = idempotency('device-health-cleanup')
      const serverScope = scopeIds(value)
      if (serverScope.length) setSuggested(serverScope)
    } catch (error) {
      setFailure({ ...explainOperatorError(error, 'The preview could not be prepared.'), stage: 'preview' })
    } finally {
      setBusy(null)
    }
  }

  const rows = preview ? alertRows(preview) : []
  const fixCandidates = preview ? errorFixCandidates(preview) : []
  const defaultAlerts = new Set(rows.filter(preselected).map((row) => row.alert_id))
  const alertIds = rows.filter((row) => selectedAlerts.has(row.alert_id)).map((row) => row.alert_id).sort((left, right) => left - right)
  const fixIds = fixCandidates.filter((id) => selectedFixes.has(id)).sort()
  const affectedDevices = new Set([...rows.filter((row) => selectedAlerts.has(row.alert_id)).map((row) => row.connector_id), ...fixIds])
  // The preview's phrase covers its default selection; any other selection
  // is described the same way from what is checked.
  const expected = preview && preview.typed_confirmation && sameSet(selectedAlerts, defaultAlerts) && sameSet(selectedFixes, new Set(fixCandidates))
    ? preview.typed_confirmation
    : cleanupConfirmation(alertIds.length, affectedDevices.size)
  const selectionProblem = !alertIds.length && !fixIds.length
    ? 'Select at least one alert or device error.'
    : alertIds.length > MAX_ALERTS
      ? `Select at most ${MAX_ALERTS} alerts per apply.`
      : fixIds.length > MAX_ERROR_FIXES ? `Select at most ${MAX_ERROR_FIXES} device errors per apply.` : ''
  const canApply = Boolean(preview) && !result && !busy && !selectionProblem
    && reason.trim().length >= 10 && typed === expected && Boolean(password)

  const apply = async (event: FormEvent) => {
    event.preventDefault()
    if (!preview || !canApply) return
    const secret = password
    setPassword('')
    setBusy('apply')
    setFailure(null)
    try {
      const value = await api<CleanupApply>('/api/v1/device-health/cleanup/apply', {
        method: 'POST',
        body: JSON.stringify({
          connector_ids: previewScope,
          digest: preview.digest,
          expires_at: preview.expires_at,
          signature: preview.signature,
          alert_ids: alertIds,
          error_fix_connector_ids: fixIds,
          reason: reason.trim(),
          typed_confirmation: typed,
          password: secret,
          idempotency_key: key.current,
        }),
      })
      setResult(value)
      toast.notice(`Stranded alert cleanup applied with audit request ${value.request_id || key.current}.`)
      // A failed refresh must not read as a failed apply.
      void Promise.resolve(onApplied?.()).catch(() => undefined)
    } catch (error) {
      if (error instanceof ApiError && error.code === 'IDEMPOTENCY_KEY_REUSED') key.current = idempotency('device-health-cleanup')
      const known = error instanceof ApiError && error.code ? applyRefusals[error.code] : undefined
      setFailure(known && error instanceof ApiError
        ? { code: error.code, summary: known, details: error.message && error.message !== known ? [error.message] : [], stage: 'apply' }
        : { ...explainOperatorError(error, 'The cleanup could not be applied.'), stage: 'apply' })
    } finally {
      setBusy(null)
    }
  }

  const toggle = <T,>(set: Set<T>, value: T, checked: boolean) => {
    const next = new Set(set)
    if (checked) next.add(value)
    else next.delete(value)
    return next
  }
  const effects = new Map((preview?.connectors || []).map((effect) => [effect.connector_id, effect]))
  const groupIds = preview ? Array.from(new Set([
    ...preview.connectors.map((effect) => effect.connector_id),
    ...rows.map((row) => row.connector_id),
  ])) : []
  const needle = search.trim().toLowerCase()
  const pickable = devices.filter((device) => !needle || `${device.display_name} ${device.zone_id} ${device.hardware_id}`.toLowerCase().includes(needle))
  const suggestedNames = suggested.map((id) => names.get(id) || id).join(' · ')

  return (
    <Dialog
      titleId="health-cleanup-title"
      title="Stranded alert review"
      description="Preview alerts left behind by earlier boots, cleared conditions or legacy acknowledgements, then resolve the ones you select with an audited reason."
      onClose={() => { if (!busy) onClose() }}
      className="health-cleanup-dialog"
    >
      <div className="dialog-body health-cleanup">
        <section aria-labelledby="health-cleanup-scope-title">
          <h3 id="health-cleanup-scope-title">1. Choose the scope</h3>
          <fieldset className="health-cleanup-scope">
            <legend className="sr-only">Cleanup scope</legend>
            <label><input type="radio" name="health-cleanup-scope" checked={scope === 'SUGGESTED'} onChange={() => setScope('SUGGESTED')} /><span><strong>Peshawar 02 + 06</strong><small>Suggested first scope · {suggestedNames}</small></span></label>
            <label><input type="radio" name="health-cleanup-scope" checked={scope === 'CHOSEN'} onChange={() => setScope('CHOSEN')} /><span><strong>Chosen devices</strong><small>Up to {MAX_SCOPE} devices · {chosen.length} selected</small></span></label>
            <label><input type="radio" name="health-cleanup-scope" checked={scope === 'ALL'} onChange={() => setScope('ALL')} /><span><strong>All devices</strong><small>Every active device except spares</small></span></label>
          </fieldset>
          {scope === 'CHOSEN' && <>
            <label className="health-cleanup-search">Find devices<input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Name, zone or MAC" /></label>
            <div className="health-cleanup-picker" role="group" aria-label="Devices to preview">
              {pickable.map((device) => <label key={device.connector_id}>
                <input
                  type="checkbox"
                  checked={chosen.includes(device.connector_id)}
                  disabled={!chosen.includes(device.connector_id) && chosen.length >= MAX_SCOPE}
                  onChange={(event) => setChosen((current) => event.target.checked ? [...current, device.connector_id] : current.filter((id) => id !== device.connector_id))}
                />
                <span><strong>{device.display_name}</strong><small>{device.zone_id} · {device.hardware_id}</small></span>
              </label>)}
              {!pickable.length && <p className="device-alerts-empty">No devices match this search.</p>}
            </div>
          </>}
          <div className="health-cleanup-step-actions">
            <button type="button" className="button primary" disabled={Boolean(busy) || (scope === 'CHOSEN' && (!chosen.length || chosen.length > MAX_SCOPE))} onClick={() => void runPreview()}>
              <Icon name="search" /> {busy === 'preview' ? 'Preparing preview…' : preview ? 'Run the preview again' : 'Preview stranded alerts'}
            </button>
            <p>The preview is read-only. Nothing changes until you apply it.</p>
          </div>
          {failure?.stage === 'preview' && <OperatorFailureMessage failure={failure} />}
        </section>

        {preview && <section aria-labelledby="health-cleanup-preview-title">
          <h3 id="health-cleanup-preview-title">2. Review the preview</h3>
          <p>{rows.length} candidate alert{rows.length === 1 ? '' : 's'} on {groupIds.length} device{groupIds.length === 1 ? '' : 's'} · {alertIds.length} selected · {preview.blocked.length} blocked · {preview.skipped.length} skipped · preview expires {dateTime(preview.expires_at)} · digest <span className="mono-value">{preview.digest.slice(0, 12)}</span></p>
          <div className="health-cleanup-devices">
            {groupIds.map((connectorId) => {
              const effect = effects.get(connectorId)
              const deviceRows = rows.filter((row) => row.connector_id === connectorId)
              const holds = holdEffectLines(effect?.hold_effects)
              const name = deviceName(connectorId, effect)
              return <article className="health-cleanup-device" key={connectorId} aria-labelledby={`health-cleanup-${connectorId}`}>
                <header>
                  <h4 id={`health-cleanup-${connectorId}`}>{name}</h4>
                  {effect && <div className="health-cleanup-effects">
                    <span>Stored <StatusBadge state={effect.lifecycle_state} /></span>
                    <span>Derived <StatusBadge state={effect.derived_lifecycle_before} /> → <StatusBadge state={effect.derived_lifecycle_after} /></span>
                    <span>Device error {errorLabel(effect.last_error_before)} → {errorLabel(effect.last_error_after)}</span>
                  </div>}
                  {holds.length > 0 && <ul className="health-cleanup-holds" aria-label={`Hold effects for ${name}`}>{holds.map((line) => <li key={line}>{line}</li>)}</ul>}
                  {Boolean(effect?.residuals?.length) && <p>Creates {effect?.residuals?.map((code) => humanizeStatus(code)).join(', ')} as a gating residual.</p>}
                </header>
                {fixCandidates.includes(connectorId) && <label className="health-cleanup-fix">
                  <input type="checkbox" checked={selectedFixes.has(connectorId)} onChange={(event) => setSelectedFixes((current) => toggle(current, connectorId, event.target.checked))} />
                  <span>Set the stored device error on {name} from {errorLabel(effect?.last_error_before)} to the derived {errorLabel(effect?.last_error_after)}</span>
                </label>}
                {deviceRows.length > 0 && <div className="health-cleanup-table" role="region" aria-label={`Candidate alerts on ${name}`} tabIndex={0}>
                  <table>
                    <thead>
                      <tr>
                        <th scope="col"><span className="sr-only">Resolve</span></th>
                        <th scope="col">Class</th>
                        <th scope="col">Alert</th>
                        <th scope="col">Raised on</th>
                        <th scope="col">Running now</th>
                        <th scope="col">Last seen</th>
                        <th scope="col">Rationale and evidence</th>
                      </tr>
                    </thead>
                    <tbody>
                      {deviceRows.map((row) => <tr key={row.alert_id}>
                        <td><input type="checkbox" checked={selectedAlerts.has(row.alert_id)} aria-label={`Resolve alert ${row.alert_id} ${humanizeStatus(row.code)}`} onChange={(event) => setSelectedAlerts((current) => toggle(current, row.alert_id, event.target.checked))} /></td>
                        <td>
                          {classLabels[normalizedStatus(row.class).toUpperCase()] || humanizeStatus(row.class)}
                          {isReview(row) && <span className="health-cleanup-tag pattern-notice">Review before selecting</span>}
                          {isDurability(row) && <span className="health-cleanup-tag pattern-waiting">storage evidence: leaves Preservation unverified</span>}
                        </td>
                        <td><strong>{humanizeStatus(row.code)}</strong><small>#{row.alert_id} · {humanizeStatus(row.state)}</small></td>
                        <td>{firmwareBoot(row.raising_firmware, row.raising_boot_id)}</td>
                        <td>{firmwareBoot(row.current_firmware, row.current_boot_id)}</td>
                        <td>{row.last_seen_at ? dateTime(row.last_seen_at) : 'Not recorded'}</td>
                        <td>{row.rationale || 'No rationale recorded.'}{evidenceText(row.evidence) && <small>{evidenceText(row.evidence)}</small>}</td>
                      </tr>)}
                    </tbody>
                  </table>
                </div>}
              </article>
            })}
            {!groupIds.length && <p className="device-alerts-empty">No stranded alerts or stale device errors in this scope.</p>}
          </div>
          {preview.blocked.length > 0 && <>
            <h4>Blocked devices (read-only)</h4>
            <ul className="health-cleanup-readonly">{preview.blocked.map((row) => <li key={row.connector_id}><strong>{deviceName(row.connector_id, effects.get(row.connector_id))}</strong> · {row.reason}</li>)}</ul>
          </>}
          {preview.skipped.length > 0 && <>
            <h4>Skipped (read-only)</h4>
            <ul className="health-cleanup-readonly">{preview.skipped.map((row) => <li key={`${row.connector_id}-${row.alert_id ?? row.code ?? row.reason}`}><strong>{deviceName(row.connector_id, effects.get(row.connector_id))}</strong>{row.code ? ` · ${humanizeStatus(row.code)}` : ''}{row.alert_id != null ? ` #${row.alert_id}` : ''} · {row.reason}</li>)}</ul>
          </>}
        </section>}

        {preview && !result && <form className="health-cleanup-form" onSubmit={(event) => void apply(event)} aria-labelledby="health-cleanup-apply-title">
          <h3 id="health-cleanup-apply-title">3. Apply</h3>
          <p>Each selected alert is resolved as CLEANUP without moving its last-seen time, and every alert, device and the summary are written to the audit log. A condition that is still real is raised again from fresh evidence.</p>
          {selectionProblem && <p role="status">{selectionProblem}</p>}
          <label>Audited reason<textarea value={reason} maxLength={500} placeholder="At least 10 characters" onChange={(event) => setReason(event.target.value)} /></label>
          <label>Type <strong>{expected}</strong><input value={typed} autoComplete="off" onChange={(event) => setTyped(event.target.value)} /></label>
          <label>Administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          {failure?.stage === 'apply' && <OperatorFailureMessage failure={failure} />}
          <div className="dialog-actions">
            {failure?.stage === 'apply' && failure.code && rerunCodes.has(failure.code) && <button type="button" className="button secondary" disabled={Boolean(busy)} onClick={() => void runPreview()}><Icon name="refresh" /> Run the preview again</button>}
            <button type="button" className="button secondary" disabled={Boolean(busy)} onClick={onClose}>Close</button>
            <button type="submit" className="button destructive" disabled={!canApply}><Icon name="shield" /> {busy === 'apply' ? 'Applying…' : 'Apply cleanup'}</button>
          </div>
        </form>}

        {result && <div className="message pattern-confirmed" role="status">
          <Icon name="check" />
          <div className="operator-failure">
            <strong>Cleanup applied{result.replayed ? ' (replayed from the stored summary)' : ''}.</strong>
            <p>Audit request <span className="mono-value">{result.request_id || key.current}</span>{result.resolved_alert_ids ? ` · ${result.resolved_alert_ids.length} alert${result.resolved_alert_ids.length === 1 ? '' : 's'} resolved` : ''}{result.residual_alert_ids?.length ? ` · ${result.residual_alert_ids.length} residual${result.residual_alert_ids.length === 1 ? '' : 's'} created` : ''}.</p>
            <p>The next heartbeat from each device re-derives its health from fresh evidence.</p>
          </div>
        </div>}
      </div>
    </Dialog>
  )
}
