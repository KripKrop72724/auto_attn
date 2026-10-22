import { useEffect, useState } from 'react'
import { api, queryString } from '../api'
import { Dialog, dateTime, type useToast } from '../App'
import { Icon } from '../Icon'
import type {
  FirmwareCampaign,
  FirmwareDeployment,
  FirmwareRetryEspState,
  FirmwareRetryPreflight,
  FirmwareRollbackDiagnosis,
  FirmwareStabilityWarning,
} from '../types'

type Toast = Pick<ReturnType<typeof useToast>, 'notice'>

export function StabilityWarnings({
  warnings,
  override,
  onOverride,
}: {
  warnings: FirmwareStabilityWarning[]
  override: boolean
  onOverride: (value: boolean) => void
}) {
  if (!warnings.length) return null
  return (
    <div className="message pattern-waiting firmware-stability-warnings" role="status">
      <Icon name="alert" />
      <div>
        <strong>ESP stability warnings</strong>
        <ul>
          {warnings.map((warning) => (
            <li key={`${warning.connector_id}-${warning.code}`}>{warning.message}</li>
          ))}
        </ul>
        <label className="firmware-override">
          <input
            type="checkbox"
            checked={override}
            onChange={(event) => onOverride(event.target.checked)}
          />{' '}
          Override the stability warnings for this attempt
        </label>
      </div>
    </div>
  )
}

const seconds = (value: number | null | undefined) =>
  value === null || value === undefined ? '?' : `${value} s`

export function RollbackEvidence({ diagnosis }: { diagnosis: FirmwareRollbackDiagnosis }) {
  const target = diagnosis.target_boot
  const resets = diagnosis.previous_firmware_resets
  const terminal = diagnosis.terminal
  const location = diagnosis.location_resets
  const reconcile = diagnosis.reconcile_at_reset?.running
  const interrupted = diagnosis.classification === 'TARGET_BOOT_INTERRUPTED'
  return (
    <section className="firmware-rollback-evidence" aria-label="Rollback evidence">
      <h4>{interrupted ? 'Target boot interrupted' : 'Return to the previous firmware'}</h4>
      <p>
        {interrupted
          ? 'The ESP left the new image before confirming it and sent no failure report. ADD cannot tell a crash from a power or reset event; the facts below are evidence, not a cause.'
          : `Not classified as an interrupted boot (${(diagnosis.unclassified_reasons || []).join(', ').toLowerCase().replaceAll('_', ' ') || 'no reason recorded'}).`}
      </p>
      {target && (
        <dl>
          <div>
            <dt>Target lifetime</dt>
            <dd>
              {seconds(target.estimated_lifetime_seconds.min)} to {seconds(target.estimated_lifetime_seconds.max)}
            </dd>
          </div>
          <div>
            <dt>Last target report</dt>
            <dd>
              {target.last_progress_at
                ? `${dateTime(target.last_progress_at)} · ${target.last_progress_code || target.last_progress_state}`
                : 'None'}
            </dd>
          </div>
          <div>
            <dt>Target-boot logs</dt>
            <dd>
              {target.logs_received} logs · {target.telemetry_received} heartbeats ·{' '}
              {target.failure_reports.length} failure reports
            </dd>
          </div>
          {resets && (
            <div>
              <dt>Resets on {diagnosis.previous_version || 'the previous firmware'}</dt>
              <dd>
                {resets.resets} in {resets.online_hours} online hours over {resets.days} days (
                {resets.reset_rate_per_online_hour}/h)
              </dd>
            </div>
          )}
          {terminal && (
            <div>
              <dt>Terminal after the reset</dt>
              <dd>
                {terminal.time_sync_after_new_boot_seconds === null
                  ? terminal.source === 'inconsistent'
                    ? 'Time sync timing inconsistent with the boot estimate'
                    : 'No time sync observed'
                  : `Time sync received ${terminal.time_sync_after_new_boot_seconds} s after the new boot${
                      terminal.answered_within_30_seconds ? ' (within 30 s)' : ' (an upper bound)'
                    }`}
              </dd>
            </div>
          )}
          {location && (
            <div>
              <dt>Other ESP resets nearby</dt>
              <dd>
                {location.resets.length
                  ? location.resets
                      .map((item) => `${item.display_name} (${item.offset_seconds > 0 ? '+' : ''}${item.offset_seconds} s)`)
                      .join(', ')
                  : `None of ${location.peers_checked} at ${location.location || 'this location'}`}{' '}
                within ±{location.window_seconds} s{location.complete ? '' : ' (still collecting)'}
              </dd>
            </div>
          )}
          <div>
            <dt>Reconcile at the reset</dt>
            <dd>{reconcile === null || reconcile === undefined ? 'Unknown' : reconcile ? 'Running' : 'Not running'}</dd>
          </div>
        </dl>
      )}
    </section>
  )
}

function EspState({ state }: { state: FirmwareRetryEspState }) {
  const faults = state.storage_io_faults ? Object.entries(state.storage_io_faults) : []
  return (
    <section className="firmware-rollback-evidence" aria-label="Current ESP state">
      <h4>Current ESP state</h4>
      <dl>
        <div>
          <dt>Connection</dt>
          <dd>
            {state.connected ? 'Online' : 'Offline'} · {state.firmware_version || 'unknown firmware'}
          </dd>
        </div>
        <div>
          <dt>Storage</dt>
          <dd>
            {!state.storage_reported
              ? 'Not reported'
              : faults.length
                ? `I/O faults: ${faults.map(([name, value]) => `${name}=${String(value)}`).join(', ')}`
                : `${state.storage_durability || 'Unknown'} · no I/O faults reported`}
          </dd>
        </div>
        <div>
          <dt>Queue</dt>
          <dd>
            {state.outbox_depth === null
              ? 'No heartbeat on this boot'
              : `${state.outbox_depth} queued at ${state.last_heartbeat_at ? dateTime(state.last_heartbeat_at) : 'the last heartbeat'}`}
          </dd>
        </div>
      </dl>
    </section>
  )
}

export function RetryTargetDialog({
  campaign,
  deployment,
  onClose,
  onChanged,
  toast,
}: {
  campaign: FirmwareCampaign
  deployment: FirmwareDeployment
  onClose: () => void
  onChanged: () => Promise<void>
  toast: Toast
}) {
  const [preflight, setPreflight] = useState<FirmwareRetryPreflight | null>(null)
  const [preflightError, setPreflightError] = useState('')
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [override, setOverride] = useState(false)
  const [pendingConfirmed, setPendingConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    // The 7-day stability scan runs only when the owner opens this dialog.
    api<FirmwareRetryPreflight>(
      `/api/v1/firmware/campaigns/${campaign.campaign_id}/retry-target${queryString({ deployment_id: deployment.deployment_id })}`,
    )
      .then((value) => {
        if (active) setPreflight(value)
      })
      .catch((reason: unknown) => {
        if (active) setPreflightError(reason instanceof Error ? reason.message : 'The retry check failed.')
      })
    return () => {
      active = false
    }
  }, [campaign.campaign_id, deployment.deployment_id])
  const warnings = preflight?.stability_warnings || []
  const pending = preflight?.pending_deployments ?? 0
  const valid =
    Boolean(preflight?.available) &&
    reason.trim().length >= 10 &&
    confirmation === deployment.target_version &&
    Boolean(password) &&
    (!warnings.length || override) &&
    (!pending || pendingConfirmed) &&
    !busy
  const retry = async () => {
    if (!preflight) return
    setBusy(true)
    setError('')
    try {
      await api(`/api/v1/firmware/campaigns/${campaign.campaign_id}/retry-target`, {
        method: 'POST',
        body: JSON.stringify({
          deployment_id: deployment.deployment_id,
          reason: reason.trim(),
          typed_confirmation: confirmation,
          password,
          override_stability_warnings: override,
          ...(override && warnings.length ? { stability_warnings_digest: preflight.stability_warnings_digest } : {}),
          // Only the number the owner confirmed; the server refuses any other.
          resume_pending_deployments: pendingConfirmed ? pending : 0,
        }),
      })
      setPassword('')
      toast.notice(
        `One new ${deployment.target_version} attempt for ${deployment.display_name || deployment.connector_id} is queued; the campaign is active again.`,
      )
      await onChanged()
      onClose()
    } catch (reason) {
      setPassword('')
      setError(reason instanceof Error ? reason.message : 'The retry was refused.')
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog
      titleId="firmware-retry-target-title"
      title="Retry same target"
      description={`${deployment.display_name || deployment.connector_id} · ${deployment.previous_version || 'unknown'} → ${deployment.target_version}`}
      onClose={onClose}
    >
      <div className="dialog-body">
        <div className="info-copy pattern-waiting">
          <Icon name="shield" />
          <div>
            <h3>One new attempt, through every admission check.</h3>
            <p>
              The ESP reset before it confirmed the new image. Retry only after checking its power,
              its reset history and its current storage and queue state below;{' '}
              {preflight?.limit ?? deployment.retry?.limit ?? 2} interrupted boots of one release stop
              retries until a UART capture is taken.
            </p>
          </div>
        </div>
        {!preflight && !preflightError && (
          <div className="message pattern-waiting" role="status">
            <Icon name="info" />
            Checking the ESP and its reset history…
          </div>
        )}
        {(preflightError || (preflight && !preflight.available)) && (
          <div className="message pattern-blocked" role="alert">
            <Icon name="alert" />
            {preflightError || preflight?.refusal || 'Retry is not available.'}
          </div>
        )}
        {preflight?.esp_state && <EspState state={preflight.esp_state} />}
        {pending > 0 && (
          <div className="message pattern-waiting" role="status">
            <Icon name="info" />
            <div>
              Retrying resumes the campaign, which also offers its {pending} other pending
              deployment{pending === 1 ? '' : 's'}, one at a time before this retry.
              <label className="firmware-override">
                <input
                  type="checkbox"
                  checked={pendingConfirmed}
                  onChange={(event) => setPendingConfirmed(event.target.checked)}
                />{' '}
                Also offer the {pending} pending deployment{pending === 1 ? '' : 's'}
              </label>
            </div>
          </div>
        )}
        <StabilityWarnings warnings={warnings} override={override} onOverride={setOverride} />
        <label>
          Audited reason
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={200}
            placeholder="10–200 characters"
          />
        </label>
        <label>
          <span>
            Type exact version <code>{deployment.target_version}</code>
          </span>
          <input
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
            autoComplete="off"
          />
        </label>
        <label>
          Administrator password
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {error && (
          <div className="message pattern-blocked" role="alert">
            <Icon name="alert" />
            {error}
          </div>
        )}
        <div className="dialog-actions">
          <button className="button secondary" onClick={onClose}>
            Keep paused
          </button>
          <button className="button primary" disabled={!valid} onClick={() => void retry()}>
            {busy ? 'Verifying…' : 'Retry same target'}
          </button>
        </div>
      </div>
    </Dialog>
  )
}
