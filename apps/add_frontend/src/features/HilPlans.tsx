import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { api } from '../api'
import { Dialog, StatusBadge, dateTime, idempotency, useToast } from '../App'
import { Icon } from '../Icon'
import type { Device, FirmwareRelease } from '../types'
import { hilAllowedTargets } from './hilTargets'
import './HilPlans.css'

type Toast = ReturnType<typeof useToast>
type PlanAction = 'pause' | 'resume' | 'cancel'

export interface HilPlanDevice {
  zone_id: string
  connector_id: string
  display_name?: string | null
  location: string
  campaign_id?: string | null
  campaign_status?: string | null
  deployment_id?: string | null
  deployment_status?: string | null
  error_code?: string | null
  connected?: boolean
  firmware_version?: string | null
  /** Holds the owner accepted at approval; any other hold halts the plan. */
  accepted_holds?: string[]
}

export interface HilPlanSlotHolder {
  connector_id: string
  zone_id?: string | null
  display_name?: string | null
  status: string
  campaign_status: string
  location: string | null
}

export interface HilPlan {
  plan_id: string
  status: 'RUNNING' | 'PAUSED' | 'COMPLETED' | 'HALTED' | 'CANCELLED' | 'EXPIRED' | string
  release: { release_id: string; version: string; image_sha256: string; application_sha256: string }
  reason: string
  approved_by: string
  approved_at: string
  expires_at: string
  stage_timeout_minutes: number
  soak_minutes?: number
  window_start_hour?: number
  window_end_hour?: number
  current_stage: number
  stage_count: number
  waiting_reason: string | null
  halt_code: string | null
  halt_reason: string | null
  stages: { index: number; status: string; devices: HilPlanDevice[] }[]
}

interface HilPlanPreview {
  release: { release_id: string; version: string; image_sha256: string; application_sha256: string }
  stages: HilPlanDevice[][]
  plan_digest: string
  expires_in_hours: number
  stage_timeout_minutes: number
  soak_minutes: number
  window_start_hour: number
  window_end_hour: number
  slot_holders: HilPlanSlotHolder[]
}

const openStates = new Set(['RUNNING', 'PAUSED'])
const message = (reason: unknown, fallback: string) => (reason instanceof Error ? reason.message : fallback)
const hour = (value: number) => `${String(value).padStart(2, '0')}:00`
const controlCopy: Record<PlanAction, { title: string; body: string }> = {
  pause: {
    title: 'No further stage starts while paused.',
    body: 'Stage devices that were never offered bytes are held too. A device already receiving bytes continues to its end, and a failure still halts the plan.',
  },
  resume: {
    title: 'The plan continues from its saved stage.',
    body: 'Held stage devices are released and the current stage timer starts again.',
  },
  cancel: {
    title: 'Cancellation ends this approval at once.',
    body: 'Campaigns that never offered bytes are cancelled. A device already receiving bytes is never interrupted: it continues under its own campaign, which you manage from the campaign list.',
  },
}

function PlanControlDialog({ plan, action, onClose, onChanged, toast }: {
  plan: HilPlan
  action: PlanAction
  onClose: () => void
  onChanged: () => Promise<void>
  toast: Toast
}) {
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submit = async () => {
    setBusy(true)
    setError('')
    try {
      await api(`/api/v1/firmware/hil-plans/${plan.plan_id}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ reason: reason.trim(), password }),
      })
      toast.notice(`Staged HIL plan ${action === 'pause' ? 'paused' : action === 'resume' ? 'resumed' : 'cancelled'} with an audit entry.`)
      await onChanged()
      onClose()
    } catch (reason) {
      setError(message(reason, 'The plan could not be changed.'))
    } finally {
      setPassword('')
      setBusy(false)
    }
  }
  return (
    <Dialog titleId="hil-plan-control-title" title={`${action[0].toUpperCase()}${action.slice(1)} staged HIL plan`}
      description={`Zone Lite ${plan.release.version} · stage ${plan.current_stage} of ${plan.stage_count}`} onClose={onClose}>
      <div className="dialog-body">
        <div className={`info-copy pattern-${action === 'cancel' ? 'blocked' : 'waiting'}`}>
          <Icon name={action === 'cancel' ? 'alert' : 'shield'} />
          <div>
            <h3>{controlCopy[action].title}</h3>
            <p>{controlCopy[action].body}</p>
          </div>
        </div>
        <label>Audited reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={200} placeholder="10–200 characters" /></label>
        <label>Administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        <div className="dialog-actions">
          <button className="button secondary" onClick={onClose}>Keep current state</button>
          <button className={`button ${action === 'cancel' ? 'destructive' : 'primary'}`} disabled={busy || reason.trim().length < 10 || !password} onClick={() => void submit()}>
            {busy ? 'Verifying…' : `Confirm ${action}`}
          </button>
        </div>
      </div>
    </Dialog>
  )
}

export function HilPlanCreator({ releases, devices, onClose, onCreated, toast }: {
  releases: FirmwareRelease[]
  devices: Device[]
  onClose: () => void
  onCreated: () => Promise<void>
  toast: Toast
}) {
  const hilReleases = releases.filter((release) => release.state === 'HIL_ONLY' && release.firmware_family !== 'hikvision' && release.hil_targets)
  const [releaseId, setReleaseId] = useState('')
  const [stages, setStages] = useState<string[][]>([['', '']])
  const [expiresInHours, setExpiresInHours] = useState(72)
  const [stageTimeout, setStageTimeout] = useState(360)
  const [soakMinutes, setSoakMinutes] = useState(30)
  const [windowStart, setWindowStart] = useState(10)
  const [windowEnd, setWindowEnd] = useState(16)
  const [preview, setPreview] = useState<HilPlanPreview | null>(null)
  // One key per previewed plan: a retried approval returns the same plan.
  const [approvalKey, setApprovalKey] = useState('')
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const release = hilReleases.find((row) => row.release_id === releaseId) || null
  const zones = useMemo(() => {
    const allowed = new Set(hilAllowedTargets(release).map((target) => target.connector_id))
    return devices
      .filter((device) => allowed.has(device.connector_id))
      .map((device) => [device.zone_id, device.display_name] as const)
      .sort(([left], [right]) => left.localeCompare(right))
  }, [devices, release])
  const chosen = stages.map((stage) => stage.filter(Boolean))
  const used = new Set(chosen.flat())
  const change = (update: () => void) => {
    update()
    setPreview(null)
    setConfirmation('')
    setPassword('')
  }
  const setZone = (stageIndex: number, slot: number, zone: string) =>
    change(() => setStages((current) => current.map((stage, index) => index === stageIndex ? stage.map((value, position) => position === slot ? zone : value) : stage)))
  const requestBody = () => ({
    release_id: releaseId, stages: chosen, expires_in_hours: expiresInHours, stage_timeout_minutes: stageTimeout,
    soak_minutes: soakMinutes, window_start_hour: windowStart, window_end_hour: windowEnd,
  })
  const runPreview = async () => {
    setBusy(true)
    setError('')
    try {
      setPreview(await api<HilPlanPreview>('/api/v1/firmware/hil-plans/preview', { method: 'POST', body: JSON.stringify(requestBody()) }))
      setApprovalKey(idempotency('hil-plan'))
    } catch (reason) {
      setError(message(reason, 'The plan could not be previewed.'))
    } finally {
      setBusy(false)
    }
  }
  const approve = async (event: FormEvent) => {
    event.preventDefault()
    if (!preview || !release) return
    setBusy(true)
    setError('')
    try {
      const plan = await api<HilPlan>('/api/v1/firmware/hil-plans', {
        method: 'POST',
        body: JSON.stringify({
          ...requestBody(), reason: reason.trim(), typed_confirmation: confirmation, password,
          plan_digest: preview.plan_digest, idempotency_key: approvalKey,
        }),
      })
      toast.notice(`Staged HIL plan approved: ${plan.stage_count} stage${plan.stage_count === 1 ? '' : 's'}; ADD starts stage 1 when its slots are free.`)
      await onCreated()
      onClose()
    } catch (reason) {
      setError(message(reason, 'The plan could not be approved.'))
    } finally {
      setPassword('')
      setConfirmation('')
      setBusy(false)
    }
  }
  const complete = chosen.length > 0 && chosen.every((stage) => stage.length > 0)
  const valid = Boolean(preview && approvalKey && release && reason.trim().length >= 10 && confirmation === release.version && password && !busy)
  return (
    <Dialog titleId="hil-plan-create-title" title="Approve a staged HIL plan"
      description="One approval with password step-up. ADD then starts each stage through the normal preflight and campaign checks, and halts on anything other than success."
      onClose={onClose} className="firmware-campaign-dialog">
      <form className="hil-plan-creator" onSubmit={(event) => void approve(event)}>
        <label>HIL-only release
          <select value={releaseId} onChange={(event) => change(() => { setReleaseId(event.target.value); setStages([['', '']]) })}>
            <option value="">Select signed bytes</option>
            {hilReleases.map((row) => <option key={row.release_id} value={row.release_id}>ZKT Zone Lite {row.version}</option>)}
          </select>
        </label>
        <fieldset disabled={!release}>
          <legend>Stages · at most two devices, in different locations</legend>
          {stages.map((stage, stageIndex) => (
            <div className="hil-plan-stage-row" key={stageIndex}>
              <strong>Stage {stageIndex + 1}</strong>
              {stage.map((value, slot) => (
                <select key={slot} aria-label={`Stage ${stageIndex + 1} device ${slot + 1}`} value={value} onChange={(event) => setZone(stageIndex, slot, event.target.value)}>
                  <option value="">{slot === 0 ? 'Select a zone' : 'Optional second zone'}</option>
                  {zones.filter(([zone]) => zone === value || !used.has(zone)).map(([zone, name]) => <option key={zone} value={zone}>{name} · {zone}</option>)}
                </select>
              ))}
              {stages.length > 1 && <button type="button" className="button text-button" onClick={() => change(() => setStages((current) => current.filter((_, index) => index !== stageIndex)))}>Remove</button>}
            </div>
          ))}
          <button type="button" className="button secondary" onClick={() => change(() => setStages((current) => [...current, ['', '']]))}><Icon name="plus" /> Add stage</button>
        </fieldset>
        <div className="hil-plan-limits">
          <label>Approval expires after (hours)<input type="number" min={1} max={72} value={expiresInHours} onChange={(event) => change(() => setExpiresInHours(Number(event.target.value)))} /></label>
          <label>Stage timeout (minutes)<input type="number" min={30} max={1440} value={stageTimeout} onChange={(event) => change(() => setStageTimeout(Number(event.target.value)))} /></label>
          <label>Soak after each install (minutes)<input type="number" min={10} max={720} value={soakMinutes} onChange={(event) => change(() => setSoakMinutes(Number(event.target.value)))} /></label>
          <label>Stages start from (hour, Pakistan time)<input type="number" min={0} max={23} value={windowStart} onChange={(event) => change(() => setWindowStart(Number(event.target.value)))} /></label>
          <label>Stages start before (hour, Pakistan time)<input type="number" min={1} max={24} value={windowEnd} onChange={(event) => change(() => setWindowEnd(Number(event.target.value)))} /></label>
        </div>
        {!preview && <button type="button" className="button secondary" disabled={!release || !complete || busy} onClick={() => void runPreview()}><Icon name="search" /> {busy ? 'Checking every stage…' : 'Preview exact plan'}</button>}
        {preview && (
          <section className="hil-plan-preview" aria-live="polite">
            <h3>Pinned plan · digest <code>{preview.plan_digest.slice(0, 12)}</code></h3>
            <p>Release {preview.release.release_id} · artifact <code>{preview.release.image_sha256.slice(0, 12)}</code> · application <code>{preview.release.application_sha256.slice(0, 12)}</code></p>
            <ol>
              {preview.stages.map((stage, index) => (
                <li key={index}>Stage {index + 1}: {stage.map((device) => `${device.display_name || device.zone_id} (${device.location}${device.connected ? '' : ', offline'})`).join(' + ')}</li>
              ))}
            </ol>
            <p>Each stage advances after every device reports SUCCEEDED and then {preview.soak_minutes} minutes on its new boot with healthy storage, a connected terminal and attendance delivery. Stages start {hour(preview.window_start_hour)}–{hour(preview.window_end_hour)} Pakistan time.</p>
            {preview.stages.flat().some((device) => device.accepted_holds?.length) && (
              <div className="message pattern-waiting">
                <Icon name="alert" />
                <div>
                  <strong>You accept these existing device conditions.</strong> Any other hold, or one of these raised again after the install, halts the plan.
                  <ul>
                    {preview.stages.flat().filter((device) => device.accepted_holds?.length).map((device) => (
                      <li key={device.zone_id}>{device.display_name || device.zone_id}: {device.accepted_holds?.join(', ')}</li>
                    ))}
                  </ul>
                </div>
              </div>
            )}
            {preview.slot_holders.length > 0 && (
              <p>Upgrade slots held now: {preview.slot_holders.map((holder) => `${holder.display_name || holder.zone_id || holder.connector_id} (${holder.status}, campaign ${holder.campaign_status.toLowerCase()})`).join('; ')}. Stages wait while these hold their slots.</p>
            )}
          </section>
        )}
        <label>Audited reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} placeholder="Why should ADD run these stages? At least 10 characters." /></label>
        <label>Type exact version {release ? <code>{release.version}</code> : <small>after choosing a release</small>}<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label>
        <label>Administrator password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
        <footer className="dialog-actions">
          <button type="button" className="button secondary" onClick={onClose}>Cancel</button>
          <button className="button primary" disabled={!valid}><Icon name="shield" /> {busy ? 'Approving…' : 'Approve staged plan'}</button>
        </footer>
      </form>
    </Dialog>
  )
}

export function HilPlansPanel({ releases, devices, hilEnabled, revision, toast, onChanged }: {
  releases: FirmwareRelease[]
  devices: Device[]
  hilEnabled: boolean
  revision: number
  toast: Toast
  onChanged?: () => Promise<void>
}) {
  const [plans, setPlans] = useState<HilPlan[]>([])
  const [error, setError] = useState('')
  const [creatorOpen, setCreatorOpen] = useState(false)
  const [control, setControl] = useState<{ plan: HilPlan; action: PlanAction } | null>(null)
  const load = useCallback(async () => {
    try {
      const response = await api<{ rows?: HilPlan[] }>('/api/v1/firmware/hil-plans')
      setPlans(Array.isArray(response?.rows) ? response.rows : [])
      setError('')
    } catch (reason) {
      setError(message(reason, 'Staged HIL plans could not be loaded.'))
    }
  }, [])
  useEffect(() => { void load() }, [load, revision])
  const changed = async () => {
    await load()
    await onChanged?.()
  }
  const open = plans.some((plan) => openStates.has(plan.status))
  return (
    <section className="panel hil-plans" aria-labelledby="hil-plans-title">
      <div className="panel-header">
        <div>
          <h2 id="hil-plans-title">Staged HIL plans</h2>
          <p>One owner approval; ADD starts each stage, waits for each install to settle, and halts on any failure, new hold, refusal or timeout.</p>
        </div>
        <button className="button primary" disabled={!hilEnabled || open} title={open ? 'Finish or cancel the open plan first' : undefined} onClick={() => setCreatorOpen(true)}>
          <Icon name="plus" /> New staged plan
        </button>
      </div>
      {error && <div className="message pattern-blocked" role="alert"><Icon name="alert" />{error}</div>}
      {!error && plans.length === 0 && <p className="hil-plans-empty">No staged HIL plan has been approved.</p>}
      {plans.map((plan) => (
        <article key={plan.plan_id} className="hil-plan-card" aria-label={`Staged HIL plan ${plan.plan_id}`}>
          <header>
            <span>
              <strong>ZKT Zone Lite {plan.release.version} · stage {plan.current_stage} of {plan.stage_count}</strong>
              <small>Approved by {plan.approved_by} {dateTime(plan.approved_at)} · expires {dateTime(plan.expires_at)} · plan {plan.plan_id.slice(0, 8)}</small>
              {plan.soak_minutes !== undefined && plan.window_start_hour !== undefined && plan.window_end_hour !== undefined && (
                <small>Soak {plan.soak_minutes} min · stage timeout {plan.stage_timeout_minutes} min · stages start {hour(plan.window_start_hour)}–{hour(plan.window_end_hour)} Pakistan time</small>
              )}
            </span>
            <StatusBadge state={plan.status} live={plan.status === 'RUNNING'} />
          </header>
          {plan.waiting_reason && <p className="message pattern-waiting"><Icon name="clock" />Waiting: {plan.waiting_reason}</p>}
          {plan.halt_reason && plan.status !== 'COMPLETED' && (
            <p className={`message pattern-${plan.status === 'CANCELLED' ? 'waiting' : 'blocked'}`} role={plan.status === 'HALTED' ? 'alert' : undefined}>
              <Icon name="alert" />{plan.halt_code}: {plan.halt_reason}
            </p>
          )}
          <ol className="hil-plan-stages">
            {plan.stages.map((stage) => (
              <li key={stage.index}>
                <span>Stage {stage.index}</span>
                <StatusBadge state={stage.status} />
                <span>{stage.devices.map((device) => `${device.display_name || device.zone_id}: ${device.deployment_status || 'not started'}${device.error_code ? ` (${device.error_code})` : ''}`).join(' · ')}</span>
              </li>
            ))}
          </ol>
          {openStates.has(plan.status) && (
            <div className="page-actions">
              {plan.status === 'RUNNING' && <button className="button secondary" onClick={() => setControl({ plan, action: 'pause' })}><Icon name="pause" /> Pause</button>}
              {plan.status === 'PAUSED' && <button className="button secondary" onClick={() => setControl({ plan, action: 'resume' })}><Icon name="power" /> Resume</button>}
              <button className="button destructive" onClick={() => setControl({ plan, action: 'cancel' })}><Icon name="x" /> Cancel plan</button>
            </div>
          )}
        </article>
      ))}
      {creatorOpen && <HilPlanCreator releases={releases} devices={devices} toast={toast} onClose={() => setCreatorOpen(false)} onCreated={changed} />}
      {control && <PlanControlDialog plan={control.plan} action={control.action} toast={toast} onClose={() => setControl(null)} onChanged={changed} />}
    </section>
  )
}
