import { useEffect, useState } from 'react'
import { api, queryString } from '../api'
import { dateTime } from '../lib/time'
import { Icon } from '../Icon'
import { Button, Callout, Dialog, TechnicalDetails } from '../ui'
import type { Toast as FullToast } from '../ui/Toast'
import { RETRY_REFUSAL_FALLBACK, plainVersion, retryRefusalSentence, stabilityWarningLine, updateActionError, updateToast } from '../words/software'
import { technicalCode } from '../words/technical'
import type {
  FirmwareCampaign,
  FirmwareDeployment,
  FirmwareRetryEspState,
  FirmwareRetryPreflight,
  FirmwareRollbackDiagnosis,
  FirmwareStabilityWarning,
} from '../types'

type Toast = Pick<FullToast, 'notice'>

// Information only: a one-click update or retry overrides exactly these
// warnings (by the server's digest) and records them with the attempt. The
// plain line is built from the structured fields; the server text stays under
// Technical details.
export function StabilityWarnings({ warnings, nameFor }: { warnings: FirmwareStabilityWarning[]; nameFor?: (connectorId: string) => string | null | undefined }) {
  if (!warnings.length) return null
  return (
    <Callout tone="warning" role="status" title="This device has been restarting a lot" className="firmware-stability-warnings">
      <ul>
        {warnings.map((warning) => (
          <li key={`${warning.connector_id}-${warning.code}`}>{stabilityWarningLine(warning, nameFor?.(warning.connector_id) || 'This device')}</li>
        ))}
      </ul>
      <TechnicalDetails>
        <ul>
          {warnings.map((warning) => <li key={`${warning.connector_id}-${warning.code}-raw`}>{warning.code}: {warning.message}</li>)}
        </ul>
      </TechnicalDetails>
    </Callout>
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
    <section className="firmware-rollback-evidence" aria-label="Rollback evidence" data-technical="">
      <h4>{interrupted ? 'Target boot interrupted' : 'Return to the previous firmware'}</h4>
      <p>
        {interrupted
          ? 'The ESP left the new image before confirming it and sent no failure report. ADD cannot tell a crash from a power or reset event; the facts below are evidence, not a cause.'
          : `Not classified as an interrupted boot (${(diagnosis.unclassified_reasons || []).map(technicalCode).join(', ') || 'no reason recorded'}).`}
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

const storageWord = (state: FirmwareRetryEspState) =>
  !state.storage_reported ? 'Not reported'
    : state.storage_io_faults && Object.keys(state.storage_io_faults).length ? 'Needs attention'
      : state.storage_durability === 'HEALTHY' || state.storage_durability === null ? 'OK' : 'Needs attention'

// "Link box now": three plain lines; the raw values go to Technical details.
function LinkBoxNow({ state }: { state: FirmwareRetryEspState }) {
  const faults = state.storage_io_faults ? Object.entries(state.storage_io_faults) : []
  return (
    <section className="firmware-retry-now" aria-label="Link box now">
      <h4>Link box now</h4>
      <ul>
        <li>{state.connected ? 'Online' : 'Offline'} · version {plainVersion(state.firmware_version) ?? 'not known'}</li>
        <li>Saved data: {storageWord(state)}</li>
        <li>Check-ins waiting to send: {state.outbox_depth === null ? 'not reported since the last restart' : state.outbox_depth}</li>
      </ul>
      <TechnicalDetails>
        <dl>
          <div><dt>Connection</dt><dd>{state.connected ? 'Online' : 'Offline'} · {state.firmware_version || 'unknown firmware'}</dd></div>
          <div><dt>Storage</dt><dd>{!state.storage_reported ? 'Not reported' : faults.length ? `I/O faults: ${faults.map(([name, value]) => `${name}=${String(value)}`).join(', ')}` : `${state.storage_durability || 'Unknown'} · no I/O faults reported`}</dd></div>
          <div><dt>Queue</dt><dd>{state.outbox_depth === null ? 'No heartbeat on this boot' : `${state.outbox_depth} queued at ${state.last_heartbeat_at ? dateTime(state.last_heartbeat_at) : 'the last heartbeat'}`}</dd></div>
          <div><dt>Boot</dt><dd>{state.boot_id || 'Not reported'}</dd></div>
        </dl>
      </TechnicalDetails>
    </section>
  )
}

export function RetryTargetDialog({
  campaign,
  deployment,
  deviceName,
  onClose,
  onChanged,
  toast,
}: {
  campaign: FirmwareCampaign
  deployment: FirmwareDeployment
  /** The device's friendly name ("Multan · Device 2"). */
  deviceName?: string
  onClose: () => void
  onChanged: () => Promise<void>
  toast: Toast
}) {
  const [preflight, setPreflight] = useState<FirmwareRetryPreflight | null>(null)
  const [preflightError, setPreflightError] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const name = deviceName || deployment.display_name || 'this device'
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
  const valid = Boolean(preflight?.available) && !busy
  // One click: the review's warnings (by digest) and pending count go with
  // the request; the server refuses if either changed since the review.
  const retry = async () => {
    if (!preflight || !valid) return
    setBusy(true)
    setError('')
    try {
      await api(`/api/v1/firmware/campaigns/${campaign.campaign_id}/retry-target`, {
        method: 'POST',
        body: JSON.stringify({
          deployment_id: deployment.deployment_id,
          override_stability_warnings: warnings.length > 0,
          ...(warnings.length ? { stability_warnings_digest: preflight.stability_warnings_digest } : {}),
          resume_pending_deployments: pending,
        }),
      })
      toast.notice(updateToast.retried(name))
      await onChanged()
      onClose()
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : ''
      const refused = retryRefusalSentence(message)
      setError(refused !== RETRY_REFUSAL_FALLBACK ? `Can't try again yet: ${refused}` : updateActionError(message))
    } finally {
      setBusy(false)
    }
  }
  const kind = preflight?.kind ?? deployment.retry?.kind
  const refusal = preflightError || (preflight && !preflight.available ? preflight.refusal || '' : null)
  return (
    <Dialog
      titleId="firmware-retry-target-title"
      title="Try the update again"
      description={`${name} · ${plainVersion(deployment.previous_version) ?? 'unknown'} → ${plainVersion(deployment.target_version) ?? deployment.target_version}`}
      onClose={onClose}
    >
      <div className="dialog-body">
        <Callout tone="info" title="One more try, with all the usual safety checks.">
          {kind === 'PARTITION_HASH_FAILED'
            ? 'The downloaded file was damaged, so it was not installed. Trying again downloads it fresh.'
            : 'The link box restarted before it finished installing. Make sure it has steady power before trying again.'}
        </Callout>
        {!preflight && !preflightError && (
          <p className="firmware-retry-checking" role="status"><Icon name="clock" /> Checking the device…</p>
        )}
        {refusal !== null && (
          <Callout tone="danger" role="alert">
            Can't try again yet: {retryRefusalSentence(refusal)}
            <TechnicalDetails><p>{refusal || 'No reason given.'}</p></TechnicalDetails>
          </Callout>
        )}
        {preflight?.esp_state && <LinkBoxNow state={preflight.esp_state} />}
        {pending > 0 && (
          <p className="firmware-retry-pending" role="status">
            Trying again also restarts the update for {pending} other waiting device{pending === 1 ? '' : 's'} here, one at a time.
          </p>
        )}
        <StabilityWarnings warnings={warnings} nameFor={() => name} />
        {error && <Callout tone="danger" role="alert">{error}</Callout>}
        <TechnicalDetails>
          <dl>
            <div><dt>Update</dt><dd>{campaign.campaign_id}</dd></div>
            <div><dt>Deployment</dt><dd>{deployment.deployment_id}</dd></div>
            <div><dt>Kind</dt><dd>{kind || 'TARGET_BOOT_INTERRUPTED'} · {preflight?.interrupted_boots ?? deployment.retry?.interrupted_boots ?? 0} of {preflight?.limit ?? deployment.retry?.limit ?? 2} tries used</dd></div>
            {deployment.error_message && <div><dt>Device message</dt><dd>{deployment.error_code} · {deployment.error_message}</dd></div>}
          </dl>
          {deployment.rollback_diagnosis && <RollbackEvidence diagnosis={deployment.rollback_diagnosis} />}
        </TechnicalDetails>
        <div className="dialog-actions">
          <Button onClick={onClose}>Keep paused</Button>
          <Button variant="primary" busy={busy} busyLabel="Starting…" disabled={!valid} onClick={() => retry()}>Try again</Button>
        </div>
      </div>
    </Dialog>
  )
}
