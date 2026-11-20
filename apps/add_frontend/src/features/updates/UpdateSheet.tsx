// Update details sheet (08-advanced 2.4, today drawer "Firmware rollout
// evidence"): the failure and Try again first, then each device's steps with
// times; every raw value sits under Technical details. Reloads quietly and
// polls every 10 s while the update runs and the tab is visible.
import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api'
import { ago, clock, dateTime, dayTime } from '../../lib/time'
import type { FirmwareDeployment } from '../../types'
import { Button, Callout, Disclosure, DivisionTag, ErrorState, Sheet, Skeleton, Spinner, StatusChip, TechnicalDetails, Timeline } from '../../ui'
import type { Toast } from '../../ui/Toast'
import {
  familyName, pauseSentence, plainVersion, readPause, retryRefusalSentence, updateActionError, updateChip, updateFailureSentence,
  updateStepWord, updateToast, downloadPercent,
} from '../../words/software'
import { RollbackEvidence } from '../FirmwareRetry'
import { CLOSED_STATES, LIVE_STATES, releaseFamily, updatePlace, visibleInterval, type NameIndex, type UpdateRow } from './model'
import { UpdateSteps } from './UpdatesTab'
import type { Catalog, RetryTarget } from './UpdatesPage'

type Action = 'pause' | 'resume' | 'cancel' | 'archive'
const FAILED = ['FAILED', 'ROLLED_BACK']
const MOVING = ['PENDING', 'OFFERED', 'DOWNLOADING', 'VERIFYING', 'READY_TO_BOOT', 'BOOTED_PENDING', 'RECONCILING']

export default function UpdateSheet({
  seed, names, catalog, toast, revision, onClose, onChanged, onRetry,
}: {
  seed: UpdateRow
  names: NameIndex
  catalog: Catalog | null
  toast: Toast
  revision: number
  onClose: () => void
  onChanged: () => Promise<void>
  onRetry: (target: RetryTarget) => void
}) {
  const [row, setRow] = useState<UpdateRow | null>(null)
  const [error, setError] = useState(false)
  const [reloading, setReloading] = useState(false)
  const [loadedAt, setLoadedAt] = useState<Date | null>(null)
  const [busy, setBusy] = useState<Action | null>(null)
  const [, tick] = useState(0)

  const load = useCallback(async () => {
    setReloading(true)
    try {
      const detail = await api<UpdateRow>(`/api/v1/firmware/campaigns/${seed.campaign_id}`)
      // The detail has no plain fields; keep the list row's (ADV-17).
      setRow({ ...detail, place_name: seed.place_name, division: seed.division, pause_kind: seed.pause_reason === detail.pause_reason ? seed.pause_kind : undefined, pause_code: seed.pause_reason === detail.pause_reason ? seed.pause_code : undefined, pause_device_name: seed.pause_reason === detail.pause_reason ? seed.pause_device_name : undefined })
      setError(false)
      setLoadedAt(new Date())
    } catch {
      setError(true)
    } finally {
      setReloading(false)
    }
  }, [seed])

  useEffect(() => { void load() }, [load, revision])
  const current = row ?? null
  useEffect(() => visibleInterval(current?.status === 'ACTIVE', 10_000, () => void load()), [current?.status, load])
  useEffect(() => {
    const timer = window.setInterval(() => tick((value) => value + 1), 5_000)
    return () => window.clearInterval(timer)
  }, [])

  const shown = current ?? seed
  const place = updatePlace(shown, names)
  const release = catalog?.rows.find((item) => item.release_id === shown.release_id)
  const family = release ? releaseFamily(release) : null
  const deployments = current?.deployments ?? []
  const first = deployments[0]
  const from = plainVersion(first?.previous_version)
  const to = plainVersion(shown.version)
  const pause = readPause(shown)
  const failures = deployments.filter((deployment) => FAILED.includes(deployment.status))
  const chip = updateChip(shown)

  const act = async (action: Action) => {
    setBusy(action)
    try {
      if (action === 'archive') await api(`/api/v1/firmware/campaigns/${shown.campaign_id}/archive`, { method: 'POST' })
      else await api(`/api/v1/firmware/campaigns/${shown.campaign_id}/${action}`, { method: 'POST', body: JSON.stringify({}) })
      toast.notice(action === 'pause' ? updateToast.paused(place.name) : action === 'resume' ? updateToast.continuing(place.name)
        : action === 'cancel' ? updateToast.stopped(place.name) : updateToast.hidden(place.name))
      await Promise.all([load(), onChanged()])
    } catch (reason) {
      toast.error(updateActionError(reason instanceof Error ? reason.message : ''))
    } finally {
      setBusy(null)
    }
  }

  const footer = current && (LIVE_STATES.includes(current.status) || (CLOSED_STATES.includes(current.status) && !current.archived_at)) ? (
    <div className="updates-sheet-actions">
      {current.status === 'ACTIVE' && <Button icon="pause" busy={busy === 'pause'} busyLabel="Pausing…" disabled={busy !== null} onClick={() => act('pause')}>Pause</Button>}
      {current.status === 'PAUSED' && (
        <Button variant="primary" icon="refresh" busy={busy === 'resume'} busyLabel="Continuing…" disabled={busy !== null || current.release_state === 'REVOKED'} onClick={() => act('resume')}>Continue</Button>
      )}
      {LIVE_STATES.includes(current.status) && <Button variant="danger-outline" icon="x" busy={busy === 'cancel'} busyLabel="Stopping…" disabled={busy !== null} onClick={() => act('cancel')}>Stop update</Button>}
      {CLOSED_STATES.includes(current.status) && !current.archived_at && <Button busy={busy === 'archive'} busyLabel="Hiding…" disabled={busy !== null} onClick={() => act('archive')}>Hide from list</Button>}
    </div>
  ) : undefined

  return (
    <Sheet
      title={`Update at ${place.name}`}
      onClose={onClose}
      className="updates-sheet"
      headerExtra={
        <div className="updates-sheet-sub">
          <DivisionTag division={place.division} />
          <span>Version {from && to && from !== to ? `${from} → ${to}` : to ?? 'not known'}{family && <> · <span data-brand>{familyName(family)}</span></>}</span>
          <StatusChip tone={chip.chip} label={chip.label} size="sm" code={shown.status} />
          {loadedAt && <span className="updates-sheet-fresh" aria-live="off">{reloading ? <Spinner size="sm" /> : null} Updated {ago(loadedAt)}</span>}
        </div>
      }
      footer={footer}
    >
      {error && !current ? (
        <ErrorState title="Couldn't load this update." onRetry={() => load()} />
      ) : !current ? (
        <Skeleton variant="block" count={3} label="Loading the update" />
      ) : (
        <>
          {error && <Callout tone="warning" role="status">Couldn't refresh this update. Showing what we had.</Callout>}
          {LIVE_STATES.includes(current.status) && pause.kind && (
            <p className="updates-card-reason">{pauseSentence(pause, { device: pause.deviceName || names.device(pause.connectorId)?.name || place.name, actor: current.actor })}</p>
          )}
          {failures.map((deployment) => {
            const name = names.device(deployment.connector_id)?.name ?? place.name
            const retry = deployment.retry
            return (
              <Callout key={`failure-${deployment.deployment_id}`} tone="danger" title={`${name} could not update`}
                action={retry?.available && current.status === 'PAUSED'
                  ? <Button variant="primary" icon="refresh" onClick={() => onRetry({ campaign: current, deployment })}>Try again on this device</Button>
                  : undefined}
              >
                <p>{updateFailureSentence(deployment.error_code, deployment.status)}</p>
                {retry && !(retry.available && current.status === 'PAUSED') && (
                  <p>Can't try again yet: {retryRefusalSentence(retry.refusal || (current.status !== 'PAUSED' ? 'The campaign must be paused to retry a target.' : ''))}</p>
                )}
              </Callout>
            )
          })}
          <section className="updates-sheet-devices" aria-label="Each device">
            <h3>{deployments.length === 1 ? 'Steps' : `${deployments.length} devices`}</h3>
            {deployments.map((deployment) => (
              <DeviceSteps key={deployment.deployment_id} deployment={deployment} row={current} names={names} fallbackName={place.name} imageSize={release?.image_size} />
            ))}
            {!deployments.length && <p>No device has started yet.</p>}
          </section>
          <TechnicalDetails>
            <dl className="updates-facts">
              <div><dt>Update ID</dt><dd>{current.campaign_id}</dd></div>
              <div><dt>Version ID</dt><dd>{current.release_id ?? 'not known'} · {current.release_state ?? ''}</dd></div>
              <div><dt>Zone</dt><dd>{current.zone_id}</dd></div>
              <div><dt>Started</dt><dd>{dateTime(current.created_at)} by {current.actor}</dd></div>
              <div><dt>Last change</dt><dd>{dateTime(current.updated_at)}</dd></div>
              <div><dt>Note</dt><dd>{current.reason}</dd></div>
              {current.pause_reason && <div><dt>Pause reason</dt><dd>{current.pause_reason}</dd></div>}
              <div><dt>Devices</dt><dd>{current.eligible} eligible · {current.legacy_skipped} skipped · {Object.entries(current.counts).map(([state, count]) => `${state} ${count}`).join(', ')}</dd></div>
            </dl>
          </TechnicalDetails>
        </>
      )}
    </Sheet>
  )
}

function DeviceSteps({ deployment, row, names, fallbackName, imageSize }: { deployment: FirmwareDeployment; row: UpdateRow; names: NameIndex; fallbackName: string; imageSize?: number | null }) {
  const name = names.device(deployment.connector_id)?.name ?? fallbackName
  const open = FAILED.includes(deployment.status) || MOVING.includes(deployment.status)
  const percent = downloadPercent({ ...deployment, image_size: imageSize ?? null })
  const rssi = deployment.transport_diagnostics?.telemetry.weakest_rssi
  const transport = deployment.transport_diagnostics
  return (
    <Disclosure defaultOpen={open} summary={<span className="updates-device-summary"><strong>{name}</strong> · {updateStepWord(deployment.status, percent)}</span>}>
      <UpdateSteps deployment={deployment} row={{ ...row, eligible: 1 }} imageSize={imageSize} />
      <Timeline
        label={`Steps for ${name}`}
        items={deployment.events.map((event, index) => ({
          id: `${event.created_at}-${index}`,
          time: dayTime(event.created_at),
          dateTime: event.created_at,
          title: updateStepWord(event.state),
          tone: FAILED.includes(event.state) ? 'offline' as const : event.state === 'SUCCEEDED' ? 'working' as const : undefined,
          technical: event.details.error_code ? <code>{String(event.details.error_code)}</code> : undefined,
        }))}
      />
      {!deployment.events.length && <p>No steps recorded yet. Last change {clock(deployment.updated_at)}.</p>}
      {typeof rssi === 'number' && rssi < -75 && <p>Wi-Fi signal: Weak</p>}
      <TechnicalDetails>
        <dl className="updates-facts">
          <div><dt>Device</dt><dd>{deployment.display_name || deployment.connector_id || 'not known'}</dd></div>
          <div><dt>MAC</dt><dd>{deployment.hardware_id ?? 'not known'}</dd></div>
          <div><dt>Deployment ID</dt><dd>{deployment.deployment_id}</dd></div>
          <div><dt>Status</dt><dd>{deployment.status}</dd></div>
          <div><dt>Version</dt><dd>{deployment.previous_version || 'Unknown'} → {deployment.target_version}</dd></div>
          <div><dt>Bytes written</dt><dd>{deployment.bytes_written.toLocaleString('en-PK')}</dd></div>
          <div><dt>Attempts</dt><dd>{deployment.attempt_count}</dd></div>
          {deployment.error_code && <div><dt>Code</dt><dd>{deployment.error_code} · {deployment.error_message || 'No additional message'}</dd></div>}
          {transport && (
            <>
              <div><dt>Download links</dt><dd>{transport.download_grants.reached_count} reached / {transport.download_grants.issued_count} issued{transport.download_grants.last_reached_at ? ` · last ${dateTime(transport.download_grants.last_reached_at)}` : ''}</dd></div>
              <div><dt>Samples</dt><dd>{transport.telemetry.sample_count}</dd></div>
              <div><dt>Lowest free memory</dt><dd>{transport.telemetry.minimum_free_heap === null ? 'Unavailable' : `${transport.telemetry.minimum_free_heap.toLocaleString('en-PK')} bytes`}</dd></div>
              <div><dt>Weakest Wi-Fi</dt><dd>{transport.telemetry.weakest_rssi === null ? 'Unavailable' : `${transport.telemetry.weakest_rssi} dBm`}</dd></div>
            </>
          )}
          {deployment.retry && <div><dt>Try again</dt><dd>{deployment.retry.available ? 'available' : deployment.retry.refusal || 'not available'} · {deployment.retry.interrupted_boots} of {deployment.retry.limit}</dd></div>}
        </dl>
        {deployment.rollback_diagnosis && <RollbackEvidence diagnosis={deployment.rollback_diagnosis} />}
      </TechnicalDetails>
    </Disclosure>
  )
}
