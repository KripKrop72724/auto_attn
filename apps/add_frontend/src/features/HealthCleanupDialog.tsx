// Clean up old problems (PRB-8; was "Stranded alert review"). The safeguards
// stay exactly as today (owner decision D-3 pending): the read-only preview
// with tick boxes, the reason, the typed phrase from the server and your
// password, one request key per preview. Only the words, the device choice and
// the layout changed: cards instead of the wide table, class chips, friendly
// names, and engineering details under Technical details.
import { useMemo, useRef, useState, type FormEvent } from 'react'
import { api, ApiError } from '../api'
import { idempotency } from '../lib/idempotency'
import { clock, dayTime } from '../lib/time'
import { Button, Dialog, DivisionTag, TechnicalDetails, formatCount, useToast } from '../ui'
import { plainStatus } from '../words/states'
import { CLEAN_UP, problemWords } from '../words/problems'
import { plural } from '../words/glossary'
import { normalizedStatus } from '../status'
import type { CleanupApply, CleanupConnectorEffect, CleanupPreview, CleanupPreviewRow, Device } from '../types'
import { deviceLabel } from './problems/ProblemCard'
import { divisionLabel } from '../lib/division'
import '../ui/filterbar.css'
import './HealthCleanupDialog.css'

type LegacyToast = { notice: (text: string) => void; error?: (text: string) => void }
type Scope = 'CHOSEN' | 'ALL'

const MAX_SCOPE = 50
const MAX_ALERTS = 200
const MAX_ERROR_FIXES = 50

/** The typed phrase for a selection other than the preview's default (the server checks it). */
export const cleanupConfirmation = (alerts: number, devices: number) => `RESOLVE ${alerts} ALERTS ON ${devices} DEVICES`

const CLASSES = ['STRANDED_DIAGNOSTICS', 'CONDITION_CLEARED', 'SUPERSEDED_ACK', 'STALE_ERROR_CODE', 'REVIEW'] as const
const rerunCodes = new Set(['PREVIEW_INVALID', 'PREVIEW_EXPIRED', 'SCOPE_CHANGED', 'SELECTION_OUT_OF_SCOPE'])

const classOf = (row: CleanupPreviewRow) => normalizedStatus(row.class).toUpperCase()
const isReview = (row: CleanupPreviewRow) => classOf(row) === 'REVIEW' || row.review === true
const isDurability = (row: CleanupPreviewRow) =>
  row.code === 'ESP_DURABILITY_FAULT' || row.residual_code === 'ESP_PRESERVATION_UNVERIFIED'
// Durability and review rows are opt-in whatever the server preselects.
const preselected = (row: CleanupPreviewRow) => row.default_selected && !isDurability(row) && !isReview(row)
const alertRows = (preview: CleanupPreview) =>
  preview.rows.filter((row): row is CleanupPreviewRow & { alert_id: number } => typeof row.alert_id === 'number')
// A stale saved device problem is per device: set it to the derived value, preselected.
const errorFixCandidates = (preview: CleanupPreview) => Array.from(new Set([
  ...preview.connectors.filter((connector) => connector.error_fix).map((connector) => connector.connector_id),
  ...preview.rows
    .filter((row) => row.alert_id == null && classOf(row) === 'STALE_ERROR_CODE')
    .map((row) => row.connector_id),
]))

const sameSet = <T,>(left: Set<T>, right: Set<T>) => left.size === right.size && [...left].every((item) => right.has(item))

const classLabel = (value: string) => plainStatus('cleanupClass', value).label
const deviceWord = (state?: string | null) => (state ? plainStatus('device', state).label : CLEAN_UP.notKnown)
const savedProblem = (code?: string | null) => (code ? problemWords({ code }).title : CLEAN_UP.noSavedProblem)

function evidenceText(value: CleanupPreviewRow['evidence']) {
  if (!value) return ''
  if (typeof value === 'string') return value.slice(0, 300)
  if (typeof value.summary === 'string') return value.summary.slice(0, 300)
  return Object.entries(value)
    .filter(([, item]) => ['string', 'number', 'boolean'].includes(typeof item))
    .slice(0, 8)
    .map(([key, item]) => `${key} ${String(item)}`)
    .join(' · ')
    .slice(0, 300)
}

function holdEffectLines(value: CleanupConnectorEffect['hold_effects']): string[] {
  if (!value) return []
  const describe = (entry: Record<string, unknown>) => Object.entries(entry).map(([key, item]) => `${key}: ${String(item)}`).join(' · ')
  if (Array.isArray(value)) return value.map((item) => (typeof item === 'string' ? item : describe(item))).filter(Boolean)
  return [describe(value)].filter(Boolean)
}

interface Failure { stage: 'preview' | 'apply'; code: string | null; summary: string; technical: string[] }

function failureFrom(stage: Failure['stage'], error: unknown): Failure {
  if (!(error instanceof ApiError)) {
    return { stage, code: null, summary: stage === 'preview' ? CLEAN_UP.previewFailed : CLEAN_UP.applyFailed, technical: error instanceof Error && error.message ? [error.message] : [] }
  }
  const technical = [error.code, error.message].filter((line): line is string => Boolean(line))
  if (error.status === 403) return { stage, code: error.code, summary: CLEAN_UP.wrongPassword, technical }
  const known = error.code ? CLEAN_UP.refusals[error.code] : undefined
  return { stage, code: error.code, summary: known || (stage === 'preview' ? CLEAN_UP.previewFailed : CLEAN_UP.applyFailed), technical }
}

export function HealthCleanupDialog({
  devices,
  toast,
  onClose,
  onApplied,
  initialChosen,
}: {
  devices: Device[]
  /** Old toast shape (the device page); the kit toast is used otherwise. */
  toast?: LegacyToast
  onClose: () => void
  onApplied?: () => Promise<void> | void
  /** Devices picked at the start (the device page passes its own). */
  initialChosen?: string[]
}) {
  const uiToast = useToast()
  const [scope, setScope] = useState<Scope>('CHOSEN')
  const [chosen, setChosen] = useState<string[]>(() => (initialChosen || []).slice(0, MAX_SCOPE))
  const [search, setSearch] = useState('')
  const [preview, setPreview] = useState<CleanupPreview | null>(null)
  const [previewScope, setPreviewScope] = useState<string[] | null>(null)
  const [selectedAlerts, setSelectedAlerts] = useState<Set<number>>(new Set())
  const [selectedFixes, setSelectedFixes] = useState<Set<string>>(new Set())
  const [classFilter, setClassFilter] = useState<string[]>([])
  const [reason, setReason] = useState('')
  const [typed, setTyped] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState<'preview' | 'apply' | null>(null)
  const [failure, setFailure] = useState<Failure | null>(null)
  const [result, setResult] = useState<CleanupApply | null>(null)
  const key = useRef(idempotency('device-health-cleanup'))
  const named = useMemo(() => new Map(devices.map((device) => [device.connector_id, deviceLabel(device)])), [devices])
  const placeOf = (connectorId: string, effect?: CleanupConnectorEffect | null) =>
    named.get(connectorId) || deviceLabel({ connector_id: connectorId, display_name: effect?.display_name || connectorId, zone_id: effect?.zone_id || effect?.display_name || '' })

  const runPreview = async () => {
    const connectorIds = scope === 'ALL' ? null : chosen
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
      setClassFilter([])
      setTyped('')
      key.current = idempotency('device-health-cleanup')
    } catch (error) {
      setFailure(failureFrom('preview', error))
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
  // is described the same way from what is ticked (the server checks it).
  const expected = preview && preview.typed_confirmation && sameSet(selectedAlerts, defaultAlerts) && sameSet(selectedFixes, new Set(fixCandidates))
    ? preview.typed_confirmation
    : cleanupConfirmation(alertIds.length, affectedDevices.size)
  const selectionProblem = !alertIds.length && !fixIds.length
    ? CLEAN_UP.pickOne
    : alertIds.length > MAX_ALERTS
      ? CLEAN_UP.pickAtMost(formatCount(MAX_ALERTS))
      : fixIds.length > MAX_ERROR_FIXES ? CLEAN_UP.pickAtMost(formatCount(MAX_ERROR_FIXES)) : ''
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
      if (toast) toast.notice(CLEAN_UP.toast)
      else uiToast.show({ tone: 'success', text: CLEAN_UP.toast })
      // A failed refresh must not read as a failed clean-up.
      void Promise.resolve(onApplied?.()).catch(() => undefined)
    } catch (error) {
      if (error instanceof ApiError && error.code === 'IDEMPOTENCY_KEY_REUSED') key.current = idempotency('device-health-cleanup')
      setFailure(failureFrom('apply', error))
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
  const classesPresent = CLASSES.filter((value) => rows.some((row) => classOf(row) === value))
  const shown = (row: CleanupPreviewRow) => !classFilter.length || classFilter.includes(classOf(row))
  const needle = search.trim().toLowerCase()
  const pickable = devices
    .filter((device) => !device.is_spare)
    .map((device) => ({ id: device.connector_id, ...deviceLabel(device), raw: `${device.display_name} ${device.zone_id}` }))
    .filter((device) => !needle || `${device.name} ${device.raw}`.toLowerCase().includes(needle))
    .sort((left, right) => left.name.localeCompare(right.name))
  const scopeReady = scope === 'ALL' || (chosen.length > 0 && chosen.length <= MAX_SCOPE)
  const failureBox = (stage: Failure['stage']) => failure?.stage === stage && (
    <div className="message pattern-blocked cleanup-failure" role="alert">
      <div>
        <strong>{failure.summary}</strong>
        {failure.technical.length > 0 && <TechnicalDetails>{failure.technical.map((line) => <p key={line}>{line}</p>)}</TechnicalDetails>}
      </div>
    </div>
  )

  return (
    <Dialog
      titleId="health-cleanup-title"
      title={CLEAN_UP.title}
      description={CLEAN_UP.description}
      onClose={() => { if (!busy) onClose() }}
      className="health-cleanup-dialog"
      size="lg"
      dirty={Boolean(reason.trim() || typed)}
    >
      <div className="dialog-body cleanup">
        <section aria-labelledby="health-cleanup-scope-title" className="cleanup-step">
          <h3 id="health-cleanup-scope-title">{CLEAN_UP.step1}</h3>
          <fieldset className="cleanup-scope">
            <legend className="sr-only">{CLEAN_UP.scopeLabel}</legend>
            <label>
              <input type="radio" name="health-cleanup-scope" checked={scope === 'CHOSEN'} onChange={() => setScope('CHOSEN')} />
              <span><strong>{CLEAN_UP.pick}</strong><small>{CLEAN_UP.pickHelp(formatCount(chosen.length))}</small></span>
            </label>
            <label>
              <input type="radio" name="health-cleanup-scope" checked={scope === 'ALL'} onChange={() => setScope('ALL')} />
              <span><strong>{CLEAN_UP.all}</strong><small>{CLEAN_UP.allHelp}</small></span>
            </label>
          </fieldset>
          {scope === 'CHOSEN' && <>
            <label className="cleanup-search">
              <span>{CLEAN_UP.find}</span>
              <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder={CLEAN_UP.findPlaceholder} />
            </label>
            <div className="cleanup-picker" role="group" aria-label={CLEAN_UP.pickerLabel}>
              {(['life', 'health'] as const).map((division) => {
                const members = pickable.filter((device) => device.division === division)
                if (!members.length) return null
                return (
                  <div key={division} className="cleanup-picker-group">
                    <p className="cleanup-picker-heading">{divisionLabel[division]} ({formatCount(members.length)})</p>
                    {members.map((device) => (
                      <label key={device.id}>
                        <input
                          type="checkbox"
                          checked={chosen.includes(device.id)}
                          disabled={!chosen.includes(device.id) && chosen.length >= MAX_SCOPE}
                          onChange={(event) => setChosen((current) => (event.target.checked ? [...current, device.id] : current.filter((id) => id !== device.id)))}
                        />
                        <span data-device-code={device.code}>{device.name}</span>
                      </label>
                    ))}
                  </div>
                )
              })}
              {!pickable.length && <p className="cleanup-empty">{CLEAN_UP.noMatch}</p>}
            </div>
          </>}
          <div className="cleanup-step-actions">
            <Button variant="primary" icon="search" busy={busy === 'preview'} busyLabel={CLEAN_UP.previewing} disabled={Boolean(busy) || !scopeReady} onClick={() => void runPreview()}>
              {preview ? CLEAN_UP.again : CLEAN_UP.preview}
            </Button>
            <p>{CLEAN_UP.readOnly}</p>
          </div>
          {failureBox('preview')}
        </section>

        {preview && <section aria-labelledby="health-cleanup-preview-title" className="cleanup-step">
          <h3 id="health-cleanup-preview-title">{CLEAN_UP.step2}</h3>
          <p>{CLEAN_UP.summary(plural(rows.length, 'old problem'), plural(groupIds.length, 'device'), formatCount(alertIds.length), clock(preview.expires_at))}</p>
          <TechnicalDetails>
            <p>{CLEAN_UP.facts.list} {preview.digest}</p>
          </TechnicalDetails>
          {classesPresent.length > 1 && (
            <div className="ui-filter-chips cleanup-classes" role="group" aria-label={CLEAN_UP.classesLabel}>
              {classesPresent.map((value) => (
                <button
                  key={value}
                  type="button"
                  className="ui-filter-chip"
                  aria-pressed={classFilter.includes(value)}
                  onClick={() => setClassFilter((current) => (current.includes(value) ? current.filter((item) => item !== value) : [...current, value]))}
                >
                  <span>{classLabel(value)}</span>{' '}
                  <span className="ui-filter-chip-count">{formatCount(rows.filter((row) => classOf(row) === value).length)}</span>
                </button>
              ))}
            </div>
          )}
          <div className="cleanup-devices">
            {groupIds.map((connectorId) => {
              const effect = effects.get(connectorId)
              const deviceRows = rows.filter((row) => row.connector_id === connectorId)
              const visibleRows = deviceRows.filter(shown)
              const fixable = fixCandidates.includes(connectorId)
              if (classFilter.length && !visibleRows.length && !(fixable && classFilter.includes('STALE_ERROR_CODE'))) return null
              const holds = holdEffectLines(effect?.hold_effects)
              const place = placeOf(connectorId, effect)
              return (
                <article className="cleanup-device" key={connectorId} aria-labelledby={`health-cleanup-${connectorId}`}>
                  <header>
                    <h4 id={`health-cleanup-${connectorId}`} className="cleanup-device-name">
                      <DivisionTag division={place.division} />
                      <span data-device-code={place.code}>{place.name}</span>
                    </h4>
                    {effect && (
                      <p className="cleanup-effects">
                        <span>{CLEAN_UP.statusNow} {deviceWord(effect.derived_lifecycle_before || effect.lifecycle_state)}</span>
                        <span>{CLEAN_UP.statusAfter} {deviceWord(effect.derived_lifecycle_after)}</span>
                        {(effect.last_error_before || effect.last_error_after) && <span>{CLEAN_UP.savedProblem(savedProblem(effect.last_error_before), savedProblem(effect.last_error_after))}</span>}
                      </p>
                    )}
                    {Boolean(effect?.residuals?.length) && <p className="cleanup-effects">{CLEAN_UP.leavesReminder}</p>}
                    {(holds.length > 0 || effect?.lifecycle_state) && (
                      <TechnicalDetails>
                        {effect?.lifecycle_state && <p>{effect.lifecycle_state} · {effect.derived_lifecycle_before} → {effect.derived_lifecycle_after}</p>}
                        {holds.map((line) => <p key={line}>{CLEAN_UP.facts.holds}: {line}</p>)}
                      </TechnicalDetails>
                    )}
                  </header>
                  {fixable && (
                    <label className="cleanup-fix">
                      <input type="checkbox" checked={selectedFixes.has(connectorId)} onChange={(event) => setSelectedFixes((current) => toggle(current, connectorId, event.target.checked))} />
                      <span>{CLEAN_UP.fixLabel(place.name, savedProblem(effect?.last_error_before), savedProblem(effect?.last_error_after))}</span>
                    </label>
                  )}
                  {visibleRows.length > 0 && (
                    <ul className="cleanup-rows">
                      {visibleRows.map((row) => {
                        const title = problemWords({ code: row.code }).title
                        return (
                          <li key={row.alert_id} className="cleanup-row">
                            <label className="cleanup-row-check">
                              <input
                                type="checkbox"
                                checked={selectedAlerts.has(row.alert_id)}
                                aria-label={CLEAN_UP.close(title)}
                                onChange={(event) => setSelectedAlerts((current) => toggle(current, row.alert_id, event.target.checked))}
                              />
                            </label>
                            <div className="cleanup-row-text">
                              <strong>{title}</strong>
                              <span className="cleanup-row-meta">
                                <span>{CLEAN_UP.why}: {classLabel(classOf(row))}</span>
                                <span>{CLEAN_UP.lastReport}: {row.last_seen_at ? dayTime(row.last_seen_at) : CLEAN_UP.notKnown}</span>
                              </span>
                              <span className="cleanup-row-explain">{plainStatus('cleanupClass', classOf(row)).explain}</span>
                              {(isReview(row) || isDurability(row)) && (
                                <span className="cleanup-tags">
                                  {isReview(row) && <span className="cleanup-tag tone-attention">{CLEAN_UP.checkFirst}</span>}
                                  {isDurability(row) && <span className="cleanup-tag tone-info">{CLEAN_UP.memoryReminder}</span>}
                                </span>
                              )}
                              <TechnicalDetails>
                                <p>{CLEAN_UP.facts.problem} {row.alert_id} · {row.code} · {row.state}</p>
                                <p>{CLEAN_UP.facts.software} {row.raising_firmware || '—'} · {CLEAN_UP.facts.restart} {row.raising_boot_id ? row.raising_boot_id.slice(0, 8) : '—'}</p>
                                <p>{CLEAN_UP.facts.softwareNow} {row.current_firmware || '—'} · {CLEAN_UP.facts.restart} {row.current_boot_id ? row.current_boot_id.slice(0, 8) : '—'}</p>
                                <p>{row.rationale || CLEAN_UP.noDetails}</p>
                                {evidenceText(row.evidence) && <p>{evidenceText(row.evidence)}</p>}
                              </TechnicalDetails>
                            </div>
                          </li>
                        )
                      })}
                    </ul>
                  )}
                </article>
              )
            })}
            {!groupIds.length && <p className="cleanup-empty">{CLEAN_UP.none}</p>}
          </div>
          {preview.blocked.length > 0 && <>
            <h4>{CLEAN_UP.blocked}</h4>
            <ul className="cleanup-readonly">{preview.blocked.map((row) => {
              const place = placeOf(row.connector_id)
              return <li key={row.connector_id}><strong data-device-code={place.code}>{place.name}</strong> · {CLEAN_UP.blockedReason}<TechnicalDetails><p>{row.reason}</p></TechnicalDetails></li>
            })}</ul>
          </>}
          {preview.skipped.length > 0 && <>
            <h4>{CLEAN_UP.skipped}</h4>
            <ul className="cleanup-readonly">{preview.skipped.map((row) => {
              const place = placeOf(row.connector_id, effects.get(row.connector_id))
              return (
                <li key={`${row.connector_id}-${row.alert_id ?? row.code ?? row.reason}`}>
                  <strong data-device-code={place.code}>{place.name}</strong>
                  {row.code ? ` · ${problemWords({ code: row.code }).title}` : ''} · {/offline/i.test(row.reason) ? CLEAN_UP.skippedOffline : CLEAN_UP.skippedOther}
                  <TechnicalDetails><p>{row.alert_id != null ? `${CLEAN_UP.facts.problem} ${row.alert_id} · ` : ''}{row.reason}</p></TechnicalDetails>
                </li>
              )
            })}</ul>
          </>}
        </section>}

        {preview && !result && <form className="cleanup-form cleanup-step" onSubmit={(event) => void apply(event)} aria-labelledby="health-cleanup-apply-title">
          <h3 id="health-cleanup-apply-title">{CLEAN_UP.step3}</h3>
          <p>{CLEAN_UP.applyIntro}</p>
          {selectionProblem && <p role="status">{selectionProblem}</p>}
          <div className="mark-fixed-field">
            <label className="mark-fixed-label" htmlFor="health-cleanup-reason">{CLEAN_UP.reason}</label>
            <textarea id="health-cleanup-reason" value={reason} rows={3} maxLength={500} placeholder={CLEAN_UP.reasonPlaceholder} onChange={(event) => setReason(event.target.value)} />
          </div>
          <div className="mark-fixed-field">
            <label className="mark-fixed-label" htmlFor="health-cleanup-typed">
              {CLEAN_UP.typePrefix} <strong data-typed-phrase="">{expected}</strong> {CLEAN_UP.typed}
            </label>
            <input id="health-cleanup-typed" value={typed} autoComplete="off" autoCapitalize="characters" spellCheck={false} onChange={(event) => setTyped(event.target.value)} />
          </div>
          <div className="mark-fixed-field">
            <label className="mark-fixed-label" htmlFor="health-cleanup-password">{CLEAN_UP.password}</label>
            <input id="health-cleanup-password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
          </div>
          {failureBox('apply')}
          <div className="dialog-actions">
            {failure?.stage === 'apply' && failure.code && rerunCodes.has(failure.code) && (
              <Button variant="secondary" icon="refresh" disabled={Boolean(busy)} onClick={() => void runPreview()}>{CLEAN_UP.again}</Button>
            )}
            <Button variant="secondary" disabled={Boolean(busy)} onClick={onClose}>{CLEAN_UP.cancel}</Button>
            <Button type="submit" variant="danger" icon="check" busy={busy === 'apply'} busyLabel={CLEAN_UP.applying} disabled={!canApply && busy !== 'apply'}>{CLEAN_UP.apply}</Button>
          </div>
        </form>}

        {result && (
          <div className="message pattern-confirmed cleanup-result" role="status">
            <div>
              <strong>{CLEAN_UP.done(plural(result.resolved_alert_ids?.length ?? 0, 'problem'), plural(result.residual_alert_ids?.length ?? 0, 'reminder'))}</strong>
              <TechnicalDetails><p>{CLEAN_UP.facts.request} {result.request_id || key.current}{result.replayed ? ' · replayed' : ''}</p></TechnicalDetails>
            </div>
          </div>
        )}
      </div>
    </Dialog>
  )
}
