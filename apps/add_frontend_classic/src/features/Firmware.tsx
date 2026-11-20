const firmwareName = (release: { release_id?: string; version?: string | null; firmware_family?: string }) =>
  `${release.firmware_family === 'hikvision' || release.release_id?.includes('hikvision') ? 'HIK' : 'ZKT'} Zone Lite ${release.version || 'unknown'}`

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react'
import './Firmware.css'
import { hilAllowedTargets, hilDevice, hilScopeLabel } from './hilTargets'
import { RetryTargetDialog, RollbackEvidence, StabilityWarnings } from './FirmwareRetry'
import { api, queryString } from '../api'
import {
  Dialog,
  Metric,
  PageHeader,
  StatusBadge,
  dateTime,
  idempotency,
  relativeTime,
  statusPattern,
  useToast,
} from '../App'
import { Icon } from '../Icon'
import type {
  Device,
  FirmwareBulkResult,
  FirmwareBulkZoneResult,
  FirmwareCampaign,
  FirmwareCampaignTotals,
  FirmwareDeployment,
  FirmwareHistoryCleared,
  FirmwareListResponse,
  FirmwareRelease,
  FirmwareReleaseTotals,
  FirmwareSection,
} from '../types'

type Toast = ReturnType<typeof useToast>
type ReleaseResponse = FirmwareListResponse<
  FirmwareRelease,
  FirmwareReleaseTotals
>
type CampaignResponse = FirmwareListResponse<
  FirmwareCampaign,
  FirmwareCampaignTotals
>
type CampaignAction = 'pause' | 'resume' | 'cancel'

const emptyReleaseTotals: FirmwareReleaseTotals = {
  all: 0,
  available: 0,
  hil_only: 0,
  revoked: 0,
}
const emptyCampaignTotals: FirmwareCampaignTotals = {
  campaigns: { all: 0 },
  deployments: { all: 0 },
}
const activeDeploymentStates = [
  'PENDING',
  'OFFERED',
  'DOWNLOADING',
  'VERIFYING',
  'READY_TO_BOOT',
  'BOOTED_PENDING',
  'RECONCILING',
]
const attentionDeploymentStates = ['FAILED', 'ROLLED_BACK', 'RELEASE_REVOKED']

const humanize = (value?: string | null) =>
  (value || 'Unknown')
    .replaceAll('_', ' ')
    .toLowerCase()
    .replace(/^./, (letter) => letter.toUpperCase())

const sumStates = (counts: Record<string, number>, states: string[]) =>
  states.reduce(
    (total, state) =>
      total + (counts[state.toLowerCase()] ?? counts[state] ?? 0),
    0,
  )

const formatBytes = (bytes: number) =>
  bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(2)} MB` : bytes >= 1024 ? `${Math.round(bytes / 1024)} KB` : `${bytes} bytes`

function FirmwareTabs({
  section,
  onChange,
}: {
  section: FirmwareSection
  onChange: (section: FirmwareSection) => void
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([])
  const tabs: Array<{ id: FirmwareSection; label: string }> = [
    { id: 'overview', label: 'Overview' },
    { id: 'prepare', label: 'Prepare device' },
    { id: 'releases', label: 'Signed releases' },
    { id: 'campaigns', label: 'Campaigns' },
  ]
  const move = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
    else if (event.key === 'ArrowLeft')
      next = (index - 1 + tabs.length) % tabs.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = tabs.length - 1
    else return
    event.preventDefault()
    onChange(tabs[next].id)
    refs.current[next]?.focus()
  }
  return (
    <div
      className="section-tabs firmware-workspace-tabs"
      role="tablist"
      aria-label="Firmware sections"
    >
      {tabs.map((tab, index) => (
        <button
          key={tab.id}
          ref={(node) => {
            refs.current[index] = node
          }}
          type="button"
          role="tab"
          aria-selected={section === tab.id}
          aria-controls={`firmware-${tab.id}-panel`}
          tabIndex={section === tab.id ? 0 : -1}
          className={section === tab.id ? 'active' : ''}
          onKeyDown={(event) => move(event, index)}
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  )
}

// The server accepts at most this many zones per one-click request; more are sent in turn.
const BULK_ZONE_LIMIT = 30
const liveCampaignStates = ['ACTIVE', 'PAUSED']
const closedCampaignStates = ['CANCELLED', 'COMPLETED']

const publishable = (release: FirmwareRelease, enabled: boolean, hilEnabled: boolean) =>
  release.state === 'AVAILABLE' ? enabled : release.state === 'HIL_ONLY' && hilEnabled

// Superseded releases are hidden, never revoked: a device's OTA path still
// verifies its running image against them. An older ADD omits the field, so
// absence keeps the release listed.
const isCurrent = (release: FirmwareRelease) => release.current !== false

const plural = (count: number, word: string) => `${count.toLocaleString()} ${word}${count === 1 ? '' : 's'}`

// Zones with a device this release may reach; the server still decides each zone.
function releaseZones(release: FirmwareRelease | null, devices: Device[]) {
  if (!release) return []
  const family = release.firmware_family
  const named = new Map<string, string>()
  devices
    .filter((device) => !device.is_spare && device.zone_id && (!family || !device.firmware_family || device.firmware_family === family))
    .forEach((device) => {
      if (!named.has(device.zone_id)) named.set(device.zone_id, device.zone_name || device.zone_id)
    })
  let ids = [...named.keys()]
  if (release.state === 'HIL_ONLY') {
    const allowed = release.hil_targets
      ? hilAllowedTargets(release).map((target) => devices.find((device) => device.connector_id === target.connector_id)?.zone_id)
      : [hilDevice(release, devices)?.zone_id]
    ids = ids.filter((id) => allowed.includes(id))
  }
  return ids.sort((left, right) => left.localeCompare(right)).map((id) => ({ id, name: named.get(id) || id }))
}

// There is no fleet-wide limit on simultaneous upgrades; a queued device
// waits only while it is offline or is not an approved fleet target.
const WAITING_TEXT: Record<string, string> = {
  NATIONWIDE_TARGET_OFFLINE: 'Waiting for the device to come online',
  NATIONWIDE_EXACT_TARGET_REQUIRED: 'Waiting: not an approved fleet target',
}
const waitingLabel = (code: string) => WAITING_TEXT[code] || `Waiting: ${humanize(code)}`

function bulkOutcome(row: FirmwareBulkZoneResult) {
  if (row.outcome === 'CREATED' && row.fleet_hold)
    return {
      badge: 'CREATED',
      pattern: 'waiting',
      text: `Created; ${waitingLabel(row.fleet_hold).toLowerCase()}.`,
    }
  if (row.outcome === 'CREATED')
    return {
      badge: 'CREATED',
      pattern: 'confirmed',
      text:
        row.message ||
        `${row.eligible} device${row.eligible === 1 ? '' : 's'} queued${row.closed.length ? '; the finished campaign was closed' : ''}.`,
    }
  return { badge: row.outcome, pattern: row.outcome === 'BUSY' ? 'waiting' : 'blocked', text: row.message || '' }
}

function UpdateFirmwareDialog({
  releases,
  devices,
  campaigns,
  enabled,
  hilEnabled,
  showOlder,
  onClose,
  onDone,
  toast,
}: {
  releases: FirmwareRelease[]
  devices: Device[]
  campaigns: FirmwareCampaign[]
  enabled: boolean
  hilEnabled: boolean
  /** "Show older releases" on Signed releases also lists superseded ones here. */
  showOlder: boolean
  onClose: () => void
  onDone: () => Promise<void>
  toast: Toast
}) {
  const options = useMemo(
    () =>
      releases.filter(
        (release) => publishable(release, enabled, hilEnabled) && (showOlder || isCurrent(release)),
      ),
    [releases, enabled, hilEnabled, showOlder],
  )
  const [releaseId, setReleaseId] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [results, setResults] = useState<FirmwareBulkZoneResult[] | null>(null)
  // The catalog is newest first: default to the newest publishable release.
  const release = options.find((row) => row.release_id === releaseId) || options[0] || null
  const zones = useMemo(() => releaseZones(release, devices), [release, devices])
  const zoneName = (id: string) => zones.find((zone) => zone.id === id)?.name || id
  const running = new Map(
    campaigns.filter((row) => liveCampaignStates.includes(row.status)).map((row) => [row.zone_id, row]),
  )
  const allSelected = zones.length > 0 && zones.every((zone) => selected.includes(zone.id))
  const chooseRelease = (value: string) => {
    setReleaseId(value)
    setSelected([])
    setResults(null)
    setError('')
  }
  const toggle = (id: string) =>
    setSelected((current) => (current.includes(id) ? current.filter((value) => value !== id) : [...current, id]))
  const update = async () => {
    if (!release || !selected.length || busy) return
    setBusy(true)
    setError('')
    setResults(null)
    const collected: FirmwareBulkZoneResult[] = []
    try {
      for (let start = 0; start < selected.length; start += BULK_ZONE_LIMIT) {
        const response = await api<FirmwareBulkResult>('/api/v1/firmware/campaigns/bulk', {
          method: 'POST',
          body: JSON.stringify({
            release_id: release.release_id,
            zone_ids: selected.slice(start, start + BULK_ZONE_LIMIT),
            idempotency_key: idempotency('firmware-bulk'),
          }),
        })
        collected.push(...response.results)
        setResults([...collected])
      }
      const created = collected.filter((row) => row.outcome === 'CREATED').length
      toast.notice(
        `${firmwareName(release)}: ${created} of ${collected.length} zone${collected.length === 1 ? '' : 's'} started.`,
      )
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The update could not be started.')
    } finally {
      setBusy(false)
      if (collected.length) await onDone()
    }
  }
  return (
    <Dialog
      titleId="firmware-update-title"
      title="Update firmware"
      description="Pick a release and the zones. One click starts a campaign in each zone."
      onClose={onClose}
      className="firmware-update-dialog"
    >
      <div className="dialog-body firmware-update">
        {!options.length ? (
          <div className="message pattern-blocked" role="alert">
            <Icon name="alert" />
            No enabled deployable release is available.
          </div>
        ) : (
          <>
            <label>
              Release
              <select value={release?.release_id || ''} onChange={(event) => chooseRelease(event.target.value)}>
                {options.map((row) => (
                  <option key={row.release_id} value={row.release_id}>
                    {firmwareName(row)} · {row.state === 'HIL_ONLY' ? 'HIL only' : 'Production'}
                  </option>
                ))}
              </select>
            </label>
            {release?.state === 'HIL_ONLY' && (
              <p className="firmware-update-hint">
                <Icon name="shield" /> {hilScopeLabel(release)}
              </p>
            )}
            <fieldset className="firmware-zone-picker">
              <legend>
                Zones <small>{selected.length} of {zones.length} selected</small>
              </legend>
              {zones.length > 0 && (
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => setSelected(allSelected ? [] : zones.map((zone) => zone.id))}
                >
                  {allSelected ? 'Clear selection' : 'Select all eligible'}
                </button>
              )}
              <div>
                {zones.map((zone) => (
                  <label key={zone.id} className="check-row">
                    <input type="checkbox" checked={selected.includes(zone.id)} onChange={() => toggle(zone.id)} />
                    <span>
                      <strong>{zone.name}</strong>
                      <small>
                        {zone.id}
                        {running.has(zone.id) ? ` · ${humanize(running.get(zone.id)?.status)} campaign` : ''}
                      </small>
                    </span>
                  </label>
                ))}
              </div>
              {!zones.length && <p className="firmware-update-hint">No zone has a device this release may update.</p>}
            </fieldset>
          </>
        )}
        {error && (
          <div className="message pattern-blocked" role="alert">
            <Icon name="alert" />
            {error}
          </div>
        )}
        {results && (
          <ul className="firmware-update-results" aria-label="Update results">
            {results.map((row) => {
              const outcome = bulkOutcome(row)
              return (
                <li key={row.zone_id} className={`pattern-${outcome.pattern}`}>
                  <StatusBadge state={outcome.badge} />
                  <span>
                    <strong>{zoneName(row.zone_id)}</strong>
                    <small>{outcome.text}</small>
                    <StabilityWarnings warnings={row.stability_warnings} />
                  </span>
                </li>
              )
            })}
          </ul>
        )}
        <div className="dialog-actions">
          <button className="button secondary" onClick={onClose}>
            {results ? 'Done' : 'Close'}
          </button>
          <button
            className="button primary"
            disabled={busy || !release || !selected.length}
            onClick={() => void update()}
          >
            <Icon name="power" />{' '}
            {busy ? 'Updating…' : `Update ${selected.length} zone${selected.length === 1 ? '' : 's'}`}
          </button>
        </div>
      </div>
    </Dialog>
  )
}

function CampaignDetailDrawer({
  seed,
  onClose,
  onControl,
  onArchive,
  acting,
  toast,
}: {
  seed: FirmwareCampaign
  onClose: () => void
  onControl: (campaign: FirmwareCampaign, action: CampaignAction) => Promise<boolean>
  onArchive: (campaign: FirmwareCampaign) => Promise<boolean>
  acting: boolean
  toast: Toast
}) {
  const [campaign, setCampaign] = useState<FirmwareCampaign | null>(null)
  const [retryTarget, setRetryTarget] = useState<FirmwareDeployment | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setCampaign(
        await api<FirmwareCampaign>(
          `/api/v1/firmware/campaigns/${seed.campaign_id}`,
        ),
      )
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : 'Campaign evidence could not be loaded.',
      )
    } finally {
      setLoading(false)
    }
  }, [seed.campaign_id])
  useEffect(() => {
    void load()
  }, [load])
  // One click; the drawer re-reads the campaign once the action succeeded.
  const act = async (run: () => Promise<boolean>) => {
    if (await run()) await load()
  }
  const row = campaign || seed
  return (
    <Dialog
      titleId="firmware-campaign-detail-title"
      title="Firmware rollout evidence"
      description={`${row.zone_name || row.zone_id} · Zone Lite ${row.version || 'unknown'}`}
      onClose={onClose}
      className="device-drawer firmware-campaign-drawer"
    >
      <div className="drawer-status">
        <StatusBadge state={row.status} />
        <span>
          {row.release_state === 'HIL_ONLY'
            ? 'Exact-MAC HIL quarantine'
            : 'Signed sequential zone rollout'}
        </span>
      </div>
      <div className="drawer-content firmware-campaign-detail">
        {loading && (
          <div className="firmware-skeleton">
            <i />
            <i />
            <i />
          </div>
        )}
        {error && (
          <div className="firmware-inline-error pattern-blocked" role="alert">
            <Icon name="alert" />
            <div>
              <strong>Campaign evidence is temporarily unavailable</strong>
              <p>{error}</p>
            </div>
            <button className="button secondary" onClick={() => void load()}>
              Retry
            </button>
          </div>
        )}
        {!loading && (
          <>
            <article
              className={`firmware-assurance-card pattern-${statusPattern(row.status)}`}
            >
              <Icon name="shield" />
              <div>
                <p className="eyebrow">ROLLOUT POSTURE</p>
                <h3>{humanize(row.status)}</h3>
                <p>
                  {row.pause_reason ||
                    `${row.eligible} eligible device${row.eligible === 1 ? '' : 's'} · ${row.legacy_skipped} excluded by the signed scope`}
                </p>
              </div>
            </article>
            <dl className="firmware-detail-facts">
              <div>
                <dt>Campaign</dt>
                <dd>
                  <code>{row.campaign_id}</code>
                </dd>
              </div>
              <div>
                <dt>Release</dt>
                <dd>
                  Zone Lite {row.version || 'unknown'} ·{' '}
                  {humanize(row.release_state)}
                </dd>
              </div>
              <div>
                <dt>Zone</dt>
                <dd>{row.zone_name || row.zone_id}</dd>
              </div>
              <div>
                <dt>Operator</dt>
                <dd>{row.actor}</dd>
              </div>
              <div>
                <dt>Created</dt>
                <dd>{dateTime(row.created_at)}</dd>
              </div>
              <div>
                <dt>Updated</dt>
                <dd>{dateTime(row.updated_at)}</dd>
              </div>
              <div className="wide">
                <dt>Audited reason</dt>
                <dd>{row.reason}</dd>
              </div>
            </dl>
            <section className="firmware-deployment-summary">
              <div className="panel-header">
                <div>
                  <h3>Deployment state</h3>
                  <p>
                    Each connector advances only after its predecessor completes
                    safely.
                  </p>
                </div>
                <StatusBadge state={`${row.deployments.length} DEVICES`} />
              </div>
              <div className="firmware-count-pills">
                {Object.entries(row.counts).map(([state, count]) => (
                  <span key={state}>
                    <strong>{count}</strong>
                    {humanize(state)}
                  </span>
                ))}
              </div>
            </section>
            <section className="firmware-deployment-list">
              {row.deployments.map((deployment) => (
                <details
                  key={deployment.deployment_id}
                  className={`firmware-deployment pattern-${statusPattern(deployment.status)}`}
                >
                  <summary>
                    <span>
                      <strong>
                        {deployment.display_name ||
                          deployment.connector_id ||
                          'Unknown connector'}
                      </strong>
                      <small>
                        {deployment.hardware_id || deployment.deployment_id}
                      </small>
                    </span>
                    <StatusBadge state={deployment.status} />
                    <Icon name="chevron" />
                  </summary>
                  <div>
                    <dl>
                      <div>
                        <dt>Version</dt>
                        <dd>
                          {deployment.previous_version || 'Unknown'} →{' '}
                          {deployment.target_version}
                        </dd>
                      </div>
                      <div>
                        <dt>Bytes written</dt>
                        <dd>{deployment.bytes_written.toLocaleString()}</dd>
                      </div>
                      <div>
                        <dt>Attempts</dt>
                        <dd>{deployment.attempt_count}</dd>
                      </div>
                      <div>
                        <dt>Last update</dt>
                        <dd>{dateTime(deployment.updated_at)}</dd>
                      </div>
                      {deployment.error_code && (
                        <div className="wide">
                          <dt>
                            {deployment.status === 'PENDING'
                              ? 'Hold'
                              : deployment.error_code === 'DOWNLOAD_INTERRUPTED'
                                ? 'Last interruption'
                                : activeDeploymentStates.includes(deployment.status)
                                  ? 'Last report'
                                  : 'Failure'}
                          </dt>
                          <dd>
                            {deployment.error_code} ·{' '}
                            {deployment.error_message ||
                              'No additional message'}
                          </dd>
                        </div>
                      )}
                      {deployment.transport_diagnostics && (
                        <>
                          <div className="wide">
                            <dt>Download endpoint</dt>
                            <dd>
                              {deployment.transport_diagnostics.download_grants
                                .endpoint_reached
                                ? `Reached ADD${
                                    deployment.transport_diagnostics
                                      .download_grants.last_reached_at
                                      ? ` · ${dateTime(
                                          deployment.transport_diagnostics
                                            .download_grants.last_reached_at,
                                        )}`
                                      : ''
                                  }`
                                : deployment.transport_diagnostics
                                      .download_grants.issued_count > 0
                                  ? 'Not reached by the ESP'
                                  : 'No download grant issued'}
                            </dd>
                          </div>
                          <div>
                            <dt>Signed grants</dt>
                            <dd>
                              {
                                deployment.transport_diagnostics.download_grants
                                  .reached_count
                              }{' '}
                              reached /{' '}
                              {
                                deployment.transport_diagnostics.download_grants
                                  .issued_count
                              }{' '}
                              issued
                            </dd>
                          </div>
                          <div>
                            <dt>Attempt telemetry</dt>
                            <dd>
                              {
                                deployment.transport_diagnostics.telemetry
                                  .sample_count
                              }{' '}
                              samples
                            </dd>
                          </div>
                          <div>
                            <dt>Minimum free heap</dt>
                            <dd>
                              {deployment.transport_diagnostics.telemetry
                                .minimum_free_heap === null
                                ? 'Unavailable'
                                : `${deployment.transport_diagnostics.telemetry.minimum_free_heap.toLocaleString()} bytes`}
                            </dd>
                          </div>
                          <div>
                            <dt>Weakest Wi-Fi</dt>
                            <dd>
                              {deployment.transport_diagnostics.telemetry
                                .weakest_rssi === null
                                ? 'Unavailable'
                                : `${deployment.transport_diagnostics.telemetry.weakest_rssi} dBm`}
                            </dd>
                          </div>
                          <div className="wide">
                            <dt>Latest ESP telemetry</dt>
                            <dd>
                              {deployment.transport_diagnostics.telemetry.latest
                                ? `${
                                    deployment.transport_diagnostics.telemetry
                                      .latest.free_heap === null
                                      ? 'heap unavailable'
                                      : `${deployment.transport_diagnostics.telemetry.latest.free_heap.toLocaleString()} bytes free`
                                  } · ${
                                    deployment.transport_diagnostics.telemetry
                                      .latest.rssi === null
                                      ? 'RSSI unavailable'
                                      : `${deployment.transport_diagnostics.telemetry.latest.rssi} dBm`
                                  } · ${dateTime(
                                    deployment.transport_diagnostics.telemetry
                                      .latest.created_at,
                                  )}`
                                : 'Unavailable'}
                            </dd>
                          </div>
                        </>
                      )}
                    </dl>
                    {deployment.rollback_diagnosis && (
                      <RollbackEvidence diagnosis={deployment.rollback_diagnosis} />
                    )}
                    {deployment.retry && (
                      <div className="firmware-retry-target">
                        {deployment.retry.available && row.status === 'PAUSED' ? (
                          <button
                            className="button primary"
                            onClick={() => setRetryTarget(deployment)}
                          >
                            <Icon name="refresh" /> Retry same target
                          </button>
                        ) : (
                          <p>
                            <Icon name="info" />{' '}
                            {deployment.retry.refusal || 'Retry is not available.'}
                          </p>
                        )}
                      </div>
                    )}
                    {deployment.events.length > 0 && (
                      <ol>
                        {deployment.events.map((event, index) => (
                          <li key={`${event.created_at}-${index}`}>
                            <time>{dateTime(event.created_at)}</time>
                            <StatusBadge state={event.state} />
                            {event.details.error_code ? (
                              <code>{String(event.details.error_code)}</code>
                            ) : (
                              <span />
                            )}
                          </li>
                        ))}
                      </ol>
                    )}
                  </div>
                </details>
              ))}
              {!row.deployments.length && (
                <div className="empty-state compact">
                  <Icon name="clock" />
                  <p>No deployment detail has been recorded yet.</p>
                </div>
              )}
            </section>
            {liveCampaignStates.includes(row.status) && (
              <footer className="firmware-drawer-actions">
                {row.status === 'ACTIVE' ? (
                  <button
                    className="button secondary"
                    disabled={acting}
                    onClick={() => void act(() => onControl(row, 'pause'))}
                  >
                    <Icon name="pause" /> Pause rollout
                  </button>
                ) : (
                  <button
                    className="button primary"
                    disabled={acting || row.release_state === 'REVOKED'}
                    title={
                      row.release_state === 'REVOKED'
                        ? 'A revoked release cannot resume.'
                        : undefined
                    }
                    onClick={() => void act(() => onControl(row, 'resume'))}
                  >
                    <Icon name="refresh" /> Resume rollout
                  </button>
                )}
                <button
                  className="button destructive"
                  disabled={acting}
                  onClick={() => void act(() => onControl(row, 'cancel'))}
                >
                  <Icon name="x" /> Cancel campaign
                </button>
              </footer>
            )}
            {closedCampaignStates.includes(row.status) && !row.archived_at && (
              <footer className="firmware-drawer-actions">
                <button
                  className="button secondary"
                  disabled={acting}
                  onClick={() => void act(() => onArchive(row))}
                >
                  <Icon name="download" /> Archive campaign
                </button>
              </footer>
            )}
          </>
        )}
      </div>
      {retryTarget && (
        <RetryTargetDialog
          campaign={row}
          deployment={retryTarget}
          onClose={() => setRetryTarget(null)}
          onChanged={load}
          toast={toast}
        />
      )}
    </Dialog>
  )
}

export function FirmwareView({
  devices,
  revision,
  toast,
  section,
  onSection,
}: {
  devices: Device[]
  revision: number
  toast: Toast
  section: FirmwareSection
  onSection: (section: FirmwareSection) => void
}) {
  const [catalogReleases, setCatalogReleases] = useState<FirmwareRelease[]>([])
  const [releaseRows, setReleaseRows] = useState<FirmwareRelease[]>([])
  const [releaseTotals, setReleaseTotals] =
    useState<FirmwareReleaseTotals>(emptyReleaseTotals)
  const [releaseFilteredTotal, setReleaseFilteredTotal] = useState(0)
  const [releaseCursor, setReleaseCursor] = useState<number | null>(null)
  const [releaseQuery, setReleaseQuery] = useState('')
  const [releaseState, setReleaseState] = useState('')
  // Off by default: only releases still in use or next in line are listed.
  const [showOlderReleases, setShowOlderReleases] = useState(false)
  const [releaseLoading, setReleaseLoading] = useState(true)
  const [releaseLoadingMore, setReleaseLoadingMore] = useState(false)
  const [releaseError, setReleaseError] = useState('')
  const [campaignRows, setCampaignRows] = useState<FirmwareCampaign[]>([])
  const [campaignTotals, setCampaignTotals] =
    useState<FirmwareCampaignTotals>(emptyCampaignTotals)
  const [campaignFilteredTotal, setCampaignFilteredTotal] = useState(0)
  const [campaignCursor, setCampaignCursor] = useState<number | null>(null)
  const [campaignQuery, setCampaignQuery] = useState('')
  const [campaignStatus, setCampaignStatus] = useState('')
  const [campaignZone, setCampaignZone] = useState('')
  const [campaignRelease, setCampaignRelease] = useState('')
  const [campaignLoading, setCampaignLoading] = useState(true)
  const [campaignLoadingMore, setCampaignLoadingMore] = useState(false)
  const [campaignError, setCampaignError] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [hilEnabled, setHilEnabled] = useState(false)
  const [updateOpen, setUpdateOpen] = useState(false)
  const [campaignDrawer, setCampaignDrawer] = useState<FirmwareCampaign | null>(
    null,
  )
  const [showArchived, setShowArchived] = useState(false)
  // The campaign (or 'history') a one-click action is running for.
  const [acting, setActing] = useState<string | null>(null)
  const [historyResult, setHistoryResult] = useState<FirmwareHistoryCleared | null>(null)
  const [revokeRelease, setRevokeRelease] = useState<FirmwareRelease | null>(
    null,
  )
  const [revokeReason, setRevokeReason] = useState('')
  const [revokePassword, setRevokePassword] = useState('')
  const [revokeBusy, setRevokeBusy] = useState(false)
  const [revokeError, setRevokeError] = useState('')
  const releaseAbortRef = useRef<AbortController | null>(null)
  const campaignAbortRef = useRef<AbortController | null>(null)
  const zones = useMemo(
    () =>
      [
        ...new Set(devices.map((device) => device.zone_id).filter(Boolean)),
      ].sort(),
    [devices],
  )

  const loadCatalog = useCallback(async () => {
    try {
      const response = await api<ReleaseResponse>(
        '/api/v1/firmware/releases?limit=200',
      )
      setCatalogReleases(response.rows)
      setEnabled(response.enabled)
      setHilEnabled(response.hil_enabled)
      setReleaseTotals(response.totals || emptyReleaseTotals)
    } catch (reason) {
      setReleaseError(
        reason instanceof Error
          ? reason.message
          : 'Signed release catalog could not be loaded.',
      )
    }
  }, [])
  const loadReleases = useCallback(
    async ({
      cursor,
      append = false,
      quiet = false,
    }: { cursor?: number; append?: boolean; quiet?: boolean } = {}) => {
      if (append) setReleaseLoadingMore(true)
      else if (!quiet) setReleaseLoading(true)
      setReleaseError('')
      releaseAbortRef.current?.abort()
      const controller = new AbortController()
      releaseAbortRef.current = controller
      try {
        const response = await api<ReleaseResponse>(
          `/api/v1/firmware/releases${queryString({ q: releaseQuery.trim() || undefined, state: releaseState || undefined, scope: showOlderReleases ? undefined : 'current', cursor, limit: 24 })}`,
          { signal: controller.signal },
        )
        setReleaseRows((current) =>
          append
            ? [
                ...new Map(
                  [...current, ...response.rows].map((row) => [
                    row.release_id,
                    row,
                  ]),
                ).values(),
              ]
            : response.rows,
        )
        setReleaseTotals(response.totals || emptyReleaseTotals)
        setReleaseFilteredTotal(response.filtered_total)
        setReleaseCursor(response.next_cursor)
        setEnabled(response.enabled)
        setHilEnabled(response.hil_enabled)
      } catch (reason) {
        if (!(reason instanceof DOMException && reason.name === 'AbortError'))
          setReleaseError(
            reason instanceof Error
              ? reason.message
              : 'Signed releases could not be loaded.',
          )
      } finally {
        if (releaseAbortRef.current === controller) {
          setReleaseLoading(false)
          setReleaseLoadingMore(false)
        }
      }
    },
    [releaseQuery, releaseState, showOlderReleases],
  )
  const loadCampaigns = useCallback(
    async ({
      cursor,
      append = false,
      quiet = false,
    }: { cursor?: number; append?: boolean; quiet?: boolean } = {}) => {
      if (append) setCampaignLoadingMore(true)
      else if (!quiet) setCampaignLoading(true)
      setCampaignError('')
      campaignAbortRef.current?.abort()
      const controller = new AbortController()
      campaignAbortRef.current = controller
      try {
        const response = await api<CampaignResponse>(
          `/api/v1/firmware/campaigns${queryString({ view: 'summary', q: campaignQuery.trim() || undefined, status: campaignStatus || undefined, zone_id: campaignZone || undefined, release_id: campaignRelease || undefined, include_archived: showArchived || undefined, cursor, limit: 24 })}`,
          { signal: controller.signal },
        )
        setCampaignRows((current) =>
          append
            ? [
                ...new Map(
                  [...current, ...response.rows].map((row) => [
                    row.campaign_id,
                    row,
                  ]),
                ).values(),
              ]
            : response.rows,
        )
        setCampaignTotals(response.totals || emptyCampaignTotals)
        setCampaignFilteredTotal(response.filtered_total)
        setCampaignCursor(response.next_cursor)
        setEnabled(response.enabled)
        setHilEnabled(response.hil_enabled)
      } catch (reason) {
        if (!(reason instanceof DOMException && reason.name === 'AbortError'))
          setCampaignError(
            reason instanceof Error
              ? reason.message
              : 'Firmware campaigns could not be loaded.',
          )
      } finally {
        if (campaignAbortRef.current === controller) {
          setCampaignLoading(false)
          setCampaignLoadingMore(false)
        }
      }
    },
    [campaignQuery, campaignStatus, campaignZone, campaignRelease, showArchived],
  )

  useEffect(() => {
    void loadCatalog()
  }, [loadCatalog, revision])
  useEffect(() => {
    const timer = window.setTimeout(() => void loadReleases(), 250)
    return () => window.clearTimeout(timer)
  }, [loadReleases])
  useEffect(() => {
    const timer = window.setTimeout(() => void loadCampaigns(), 250)
    return () => window.clearTimeout(timer)
  }, [loadCampaigns])
  useEffect(() => {
    if (section === 'releases') void loadReleases({ quiet: true })
    if (section === 'campaigns' || section === 'overview')
      void loadCampaigns({ quiet: true })
  }, [revision]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(
    () => () => {
      releaseAbortRef.current?.abort()
      campaignAbortRef.current?.abort()
    },
    [],
  )

  const revoke = async () => {
    if (!revokeRelease || revokeReason.trim().length < 10 || !revokePassword)
      return
    setRevokeBusy(true)
    setRevokeError('')
    try {
      await api(
        `/api/v1/firmware/releases/${revokeRelease.release_id}/revoke`,
        {
          method: 'POST',
          body: JSON.stringify({
            reason: revokeReason.trim(),
            password: revokePassword,
          }),
        },
      )
      toast.notice(
        `Release ${revokeRelease.version} is revoked and active campaigns are paused.`,
      )
      setRevokePassword('')
      setRevokeReason('')
      setRevokeRelease(null)
      await Promise.all([
        loadCatalog(),
        loadReleases({ quiet: true }),
        loadCampaigns({ quiet: true }),
      ])
    } catch (reason) {
      setRevokePassword('')
      setRevokeError(
        reason instanceof Error
          ? reason.message
          : 'Firmware release could not be revoked.',
      )
    } finally {
      setRevokeBusy(false)
    }
  }
  // One click each: the server checks the campaign's state (409 otherwise).
  const controlCampaign = async (campaign: FirmwareCampaign, action: CampaignAction) => {
    setActing(campaign.campaign_id)
    try {
      await api(`/api/v1/firmware/campaigns/${campaign.campaign_id}/${action}`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      toast.notice(
        `${campaign.zone_name || campaign.zone_id}: campaign ${action === 'pause' ? 'paused' : action === 'resume' ? 'resumed' : 'cancelled'}.`,
      )
      await loadCampaigns({ quiet: true })
      return true
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : 'Campaign control failed.')
      return false
    } finally {
      setActing(null)
    }
  }
  const archiveCampaign = async (campaign: FirmwareCampaign) => {
    setActing(campaign.campaign_id)
    try {
      await api(`/api/v1/firmware/campaigns/${campaign.campaign_id}/archive`, { method: 'POST' })
      toast.notice(`${campaign.zone_name || campaign.zone_id}: campaign archived.`)
      await loadCampaigns({ quiet: true })
      return true
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : 'The campaign could not be archived.')
      return false
    } finally {
      setActing(null)
    }
  }
  const archiveAllClosed = async () => {
    setActing('history')
    try {
      const result = await api<{ archived: number }>('/api/v1/firmware/campaigns/archive-cancelled', {
        method: 'POST',
      })
      toast.notice(
        result.archived
          ? `${result.archived} cancelled or completed campaign${result.archived === 1 ? '' : 's'} archived.`
          : 'No cancelled or completed campaign to archive.',
      )
      await loadCampaigns({ quiet: true })
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : 'Campaigns could not be archived.')
    } finally {
      setActing(null)
    }
  }
  const clearHistory = async () => {
    setActing('history')
    try {
      const result = await api<FirmwareHistoryCleared>('/api/v1/firmware/campaigns/clear-history', {
        method: 'POST',
      })
      setHistoryResult(result)
      toast.notice(
        `Firmware history cleared: ${result.cancelled} cancelled, ${result.deleted} removed, ${result.archived} archived.`,
      )
      await Promise.all([loadCatalog(), loadCampaigns({ quiet: true })])
    } catch (reason) {
      toast.error(reason instanceof Error ? reason.message : 'Firmware history could not be cleared.')
    } finally {
      setActing(null)
    }
  }
  const copy = async (label: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      toast.notice(`${label} copied.`)
    } catch {
      toast.error(`${label} could not be copied.`)
    }
  }

  const productionRelease =
    catalogReleases.find((release) => release.state === 'AVAILABLE') || null
  const hilRelease =
    catalogReleases.find((release) => release.state === 'HIL_ONLY') || null
  const otaReady = devices.filter(
    (device) => device.ota_state === 'OTA_READY',
  ).length
  const activeDeployments = sumStates(
    campaignTotals.deployments,
    activeDeploymentStates,
  )
  const attentionItems =
    (campaignTotals.campaigns.paused || 0) +
    sumStates(campaignTotals.deployments, attentionDeploymentStates) +
    devices.filter((device) =>
      ['ROLLBACK_REQUIRED', 'OTA_BLOCKED'].includes(device.ota_state || ''),
    ).length
  const canCreate = catalogReleases.some((release) =>
    release.state === 'AVAILABLE'
      ? enabled
      : release.state === 'HIL_ONLY' && hilEnabled,
  )
  const releaseFilterCount =
    Number(Boolean(releaseQuery.trim())) + Number(Boolean(releaseState))
  const hiddenReleases = releaseTotals.hidden ?? 0
  const campaignFilterCount =
    Number(Boolean(campaignQuery.trim())) +
    Number(Boolean(campaignStatus)) +
    Number(Boolean(campaignZone)) +
    Number(Boolean(campaignRelease))
  const activeCampaigns = campaignRows.filter((campaign) =>
    ['ACTIVE', 'PAUSED'].includes(campaign.status),
  )

  return (
    <div className="firmware-workspace">
      <PageHeader
        title="Firmware"
        description="Signed releases, device preparation, and audited rollouts to exact device scopes."
        action={
          <div className="page-actions">
            <button
              className="button secondary"
              onClick={() =>
                void Promise.all([
                  loadCatalog(),
                  loadReleases({ quiet: true }),
                  loadCampaigns({ quiet: true }),
                ])
              }
            >
              <Icon name="refresh" /> Refresh
            </button>
            <button
              className="button primary"
              disabled={!canCreate}
              title={
                !canCreate
                  ? 'No enabled deployable release channel is available.'
                  : undefined
              }
              onClick={() => setUpdateOpen(true)}
            >
              <Icon name="power" /> Update firmware
            </button>
          </div>
        }
      />
      <FirmwareTabs
        section={section}
        onChange={onSection}
      />
      {section !== 'prepare' && <section className="firmware-channel-strip" aria-label="OTA channels">
        <article className={`pattern-${enabled ? 'confirmed' : 'blocked'}`}>
          <Icon name="shield" />
          <span>
            <strong>National production OTA</strong>
            <small>
              {enabled
                ? 'Available releases may target an exact zone'
                : 'Disabled; production campaign creation is blocked'}
            </small>
          </span>
          <StatusBadge state={enabled ? 'ENABLED' : 'DISABLED'} />
        </article>
        <article className={`pattern-${hilEnabled ? 'confirmed' : 'blocked'}`}>
          <Icon name="terminal" />
          <span>
            <strong>Quarantined HIL OTA</strong>
            <small>
              {hilEnabled
                ? 'HIL-only bytes remain bound to one exact MAC'
                : 'Disabled; HIL campaigns cannot start'}
            </small>
          </span>
          <StatusBadge state={hilEnabled ? 'ENABLED' : 'DISABLED'} />
        </article>
      </section>}
      {section === 'overview' && (
        <div role="tabpanel" id="firmware-overview-panel">
          <section className="metric-grid firmware-metrics">
            <Metric
              label="Production release"
              value={
                productionRelease ? `v${productionRelease.version}` : 'None'
              }
              detail={
                productionRelease
                  ? `Published ${relativeTime(productionRelease.published_at)}`
                  : 'No AVAILABLE signed release'
              }
              icon="shield"
              tone={productionRelease ? 'positive' : 'warning'}
            />
            <Metric
              label="OTA-ready fleet"
              value={`${otaReady}/${devices.length}`}
              detail="Devices reporting OTA ready"
              icon="server"
              tone={
                otaReady === devices.length && devices.length
                  ? 'positive'
                  : 'warning'
              }
            />
            <Metric
              label="Active deployments"
              value={activeDeployments.toLocaleString()}
              detail="Pending through reconciliation"
              icon="refresh"
              tone={activeDeployments ? 'positive' : 'neutral'}
            />
            <Metric
              label="Needs attention"
              value={attentionItems.toLocaleString()}
              detail="Paused, failed, rolled back, or blocked"
              icon="alert"
              tone={attentionItems ? 'critical' : 'positive'}
            />
          </section>
          <section className="firmware-overview-grid">
            <article className="firmware-release-hero">
              <div>
                <p className="eyebrow">NATIONAL PRODUCTION CHANNEL</p>
                <h2>
                  {productionRelease
                    ? firmwareName(productionRelease)
                    : 'No production release available'}
                </h2>
                <p>
                  {productionRelease
                    ? <>Signed by <code>{productionRelease.signing_key_id}</code> · {productionRelease.partition_layout} · {dateTime(productionRelease.published_at)}</>
                    : 'Production rollout remains blocked until a protected workflow publishes and promotes signed bytes.'}
                </p>
                <div className="page-actions">
                  <button
                    className="button primary"
                    onClick={() => onSection('prepare')}
                  >
                    <Icon name="plus" /> Prepare an ESP32
                  </button>
                  <button
                    className="button secondary"
                    onClick={() => onSection('releases')}
                  >
                    Review release inventory <Icon name="chevron" />
                  </button>
                </div>
              </div>
              <Icon name="shield" />
            </article>
            <article className="firmware-channel-card">
              <p className="eyebrow">HIL QUARANTINE</p>
              <h3>
                {hilRelease
                  ? firmwareName(hilRelease)
                  : 'No active HIL candidate'}
              </h3>
              <p>
                {hilRelease
                  ? `${hilScopeLabel(hilRelease)}. Restricted to HIL devices.`
                  : 'HIL publishing remains a protected external workflow.'}
              </p>
              <StatusBadge state={hilRelease?.state || 'NO CANDIDATE'} />
            </article>
            <article className="firmware-channel-card">
              <p className="eyebrow">FLEET READINESS</p>
              <h3>
                {otaReady.toLocaleString()} devices can receive signed OTA
              </h3>
              <div className="firmware-readiness-bar">
                <i
                  style={{
                    width: `${devices.length ? Math.round((otaReady / devices.length) * 100) : 0}%`,
                  }}
                />
              </div>
              <p>
                {
                  devices.filter((device) => device.ota_state === 'UPDATING')
                    .length
                }{' '}
                updating ·{' '}
                {
                  devices.filter((device) =>
                    ['ROLLBACK_REQUIRED', 'OTA_BLOCKED'].includes(
                      device.ota_state || '',
                    ),
                  ).length
                }{' '}
                blocked ·{' '}
                {
                  devices.filter(
                    (device) => device.ota_state === 'LEGACY_MANUAL_UPDATE',
                  ).length
                }{' '}
                manual-only
              </p>
            </article>
            <section className="panel firmware-active-rollouts">
              <div className="panel-header">
                <div>
                  <h2>Active rollout posture</h2>
                  <p>
                    Most recent active and paused campaigns, ordered by server
                    history.
                  </p>
                </div>
                <button
                  className="button secondary"
                  onClick={() => onSection('campaigns')}
                >
                  View all campaigns
                </button>
              </div>
              {activeCampaigns.slice(0, 4).map((campaign) => (
                <button
                  key={campaign.campaign_id}
                  onClick={() => setCampaignDrawer(campaign)}
                >
                  <span>
                    <strong>{campaign.zone_name || campaign.zone_id}</strong>
                    <small>
                      Zone Lite {campaign.version || 'unknown'} ·{' '}
                      {dateTime(campaign.updated_at)}
                    </small>
                  </span>
                  <span>
                    <strong>
                      {sumStates(campaign.counts, activeDeploymentStates)}
                    </strong>
                    <small>
                      {campaign.status === 'ACTIVE' && campaign.waiting
                        ? waitingLabel(campaign.waiting.code).toLowerCase()
                        : 'deployments in progress'}
                    </small>
                  </span>
                  <StatusBadge state={campaign.status} />
                  <Icon name="chevron" />
                </button>
              ))}
              {!campaignLoading && !activeCampaigns.length && (
                <div className="empty-state compact">
                  <Icon name="shield" />
                  <p>No active firmware rollout requires attention.</p>
                </div>
              )}
            </section>
          </section>
        </div>
      )}
      {section === 'releases' && (
        <section
          className="panel firmware-inventory"
          role="tabpanel"
          id="firmware-releases-panel"
        >
          <div className="panel-header">
            <div>
              <h2>Signed release inventory</h2>
              <p>
                {showOlderReleases
                  ? 'Production, HIL-only, and revoked releases with their signing evidence.'
                  : 'Releases devices run, live campaigns use, the newest production release, and newer HIL candidates. Older releases are hidden, not revoked.'}
              </p>
            </div>
            <div className="firmware-history-actions">
              {!showOlderReleases && hiddenReleases > 0 && (
                <small>{plural(hiddenReleases, 'older release')} hidden</small>
              )}
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={showOlderReleases}
                  onChange={(event) => setShowOlderReleases(event.target.checked)}
                />{' '}
                Show older releases
              </label>
              <span className="badge">
                {releaseFilteredTotal.toLocaleString()} matching
              </span>
            </div>
          </div>
          <div className="firmware-toolbar">
            <label className="search-field">
              <span className="sr-only">Search signed releases</span>
              <Icon name="search" />
              <input
                value={releaseQuery}
                onChange={(event) => setReleaseQuery(event.target.value)}
                placeholder="Search version, release ID, Git SHA, signing key, or hash"
              />
            </label>
            <label>
              <span className="sr-only">Channel</span>
              <select
                value={releaseState}
                onChange={(event) => setReleaseState(event.target.value)}
              >
                <option value="">All channels</option>
                <option value="AVAILABLE">Production</option>
                <option value="HIL_ONLY">HIL only</option>
                <option value="REVOKED">Revoked</option>
              </select>
            </label>
            {releaseFilterCount > 0 && (
              <button
                className="button secondary"
                onClick={() => {
                  setReleaseQuery('')
                  setReleaseState('')
                }}
              >
                Clear filters
              </button>
            )}
          </div>
          {releaseError && (
            <div className="firmware-inline-error pattern-blocked" role="alert">
              <Icon name="alert" />
              <div>
                <strong>Latest release inventory could not be loaded</strong>
                <p>
                  {releaseError}
                  {releaseRows.length
                    ? ' Existing results remain visible.'
                    : ''}
                </p>
              </div>
              <button
                className="button secondary"
                onClick={() => void loadReleases()}
              >
                Retry
              </button>
            </div>
          )}
          {releaseLoading && !releaseRows.length && (
            <div className="firmware-skeleton">
              <i />
              <i />
              <i />
            </div>
          )}
          <div className="firmware-release-list">
            {releaseRows.map((release) => (
              <article
                key={release.release_id}
                className={`firmware-release-card pattern-${statusPattern(release.state)}`}
              >
                <header>
                  <span>
                    <p className="eyebrow">
                      {release.state === 'AVAILABLE'
                        ? 'PRODUCTION RELEASE'
                        : release.state === 'HIL_ONLY'
                          ? 'EXACT-MAC HIL CANDIDATE'
                          : 'REVOKED RELEASE'}
                    </p>
                    <h3>{firmwareName(release)}</h3>
                    <small>{release.release_id}</small>
                  </span>
                  <span className="firmware-campaign-badges">
                    {typeof release.devices_running === 'number' && (
                      <span
                        className="badge"
                        title="Active devices that report this firmware version"
                      >
                        {plural(release.devices_running, 'device')}
                      </span>
                    )}
                    <StatusBadge state={release.state} />
                  </span>
                </header>
                <div className="firmware-release-primary">
                  <span>
                    <small>Published</small>
                    <strong>{dateTime(release.published_at)}</strong>
                  </span>
                  <span>
                    <small>Signing key</small>
                    <strong>{release.signing_key_id}</strong>
                  </span>
                  <span>
                    <small>Image size</small>
                    <strong title={`${release.image_size.toLocaleString()} bytes`}>{formatBytes(release.image_size)}</strong>
                  </span>
                  <span>
                    <small>Partition</small>
                    <strong>{release.partition_layout}</strong>
                  </span>
                </div>
                {release.state === 'HIL_ONLY' && (
                  <div className="firmware-release-quarantine">
                    <Icon name="shield" />
                    <span>
                      <strong>Quarantined target</strong>
                      <small>
                        {hilScopeLabel(release)}
                      </small>
                    </span>
                  </div>
                )}
                <details>
                  <summary>Cryptographic identity and provenance</summary>
                  <dl>
                    <div>
                      <dt>Git commit</dt>
                      <dd>
                        <code>{release.git_sha}</code>
                        <button
                          aria-label={`Copy Git commit for Zone Lite ${release.version}`}
                          onClick={() =>
                            void copy('Git commit', release.git_sha)
                          }
                        >
                          <Icon name="copy" />
                        </button>
                      </dd>
                    </div>
                    <div>
                      <dt>Image SHA-256</dt>
                      <dd>
                        <code>{release.image_sha256}</code>
                        <button
                          aria-label={`Copy image hash for Zone Lite ${release.version}`}
                          onClick={() =>
                            void copy('Image SHA-256', release.image_sha256)
                          }
                        >
                          <Icon name="copy" />
                        </button>
                      </dd>
                    </div>
                    <div>
                      <dt>Application SHA-256</dt>
                      <dd>
                        <code>
                          {release.application_sha256 || 'Unavailable'}
                        </code>
                        {release.application_sha256 && (
                          <button
                            aria-label={`Copy application hash for Zone Lite ${release.version}`}
                            onClick={() =>
                              void copy(
                                'Application SHA-256',
                                release.application_sha256 || '',
                              )
                            }
                          >
                            <Icon name="copy" />
                          </button>
                        )}
                      </dd>
                    </div>
                    {release.revoked_at && (
                      <div>
                        <dt>Revoked</dt>
                        <dd>
                          {dateTime(release.revoked_at)} by{' '}
                          {release.revoked_by || 'an administrator'}
                        </dd>
                      </div>
                    )}
                  </dl>
                </details>
                {release.state !== 'REVOKED' && (
                  <footer>
                    <button
                      className="button destructive-outline"
                      onClick={() => {
                        setRevokeRelease(release)
                        setRevokeError('')
                      }}
                    >
                      <Icon name="alert" /> Revoke release
                    </button>
                  </footer>
                )}
              </article>
            ))}
            {!releaseLoading && !releaseRows.length && (
              <div className="empty-state">
                <Icon name="terminal" />
                <h3>
                  {releaseFilterCount
                    ? 'No signed releases match these filters.'
                    : !showOlderReleases && hiddenReleases > 0
                      ? 'No current signed releases. Older releases are hidden.'
                      : 'No signed firmware releases are published.'}
                </h3>
                <p>
                  Signing, key custody, binary upload, and promotion remain
                  protected workflows outside ADD.
                </p>
              </div>
            )}
          </div>
          {releaseCursor && (
            <div className="load-more">
              <button
                className="button secondary"
                disabled={releaseLoadingMore}
                onClick={() =>
                  void loadReleases({
                    cursor: releaseCursor,
                    append: true,
                    quiet: true,
                  })
                }
              >
                {releaseLoadingMore
                  ? 'Loading more releases…'
                  : 'Load more releases'}
              </button>
              <small>
                {releaseRows.length.toLocaleString()} of{' '}
                {releaseFilteredTotal.toLocaleString()} matching releases loaded
              </small>
            </div>
          )}
        </section>
      )}
      {section === 'campaigns' && (
        <section
          className="panel firmware-campaigns"
          role="tabpanel"
          id="firmware-campaigns-panel"
        >
          <div className="panel-header">
            <div>
              <h2>Campaign operations</h2>
              <p>
                Sequential rollouts, attention first. Open a campaign for its
                per-device evidence.
              </p>
            </div>
            <div className="firmware-history-actions">
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={showArchived}
                  onChange={(event) => setShowArchived(event.target.checked)}
                />{' '}
                Show archived{campaignTotals.archived_campaigns ? ` (${campaignTotals.archived_campaigns.toLocaleString()})` : ''}
              </label>
              <button
                className="button secondary"
                disabled={acting !== null}
                onClick={() => void archiveAllClosed()}
              >
                <Icon name="download" /> Archive all cancelled
              </button>
              <button
                className="button destructive-outline"
                disabled={acting !== null}
                title="Cancels every campaign that is not mid-install, then removes never-offered attempts and archives the rest. Evidence the OTA checks read is kept."
                onClick={() => void clearHistory()}
              >
                <Icon name="trash" /> {acting === 'history' ? 'Clearing…' : 'Clear firmware history'}
              </button>
              <span className="badge">
                {campaignFilteredTotal.toLocaleString()} matching
              </span>
            </div>
          </div>
          {historyResult && (
            <div className="message pattern-confirmed firmware-history-result" role="status">
              <Icon name="check" />
              <div>
                <strong>Firmware history cleared</strong>
                <p>
                  {historyResult.completed} completed · {historyResult.cancelled} cancelled ·{' '}
                  {historyResult.deleted} removed · {historyResult.archived} archived
                  {historyResult.more ? ' · more remain, clear again' : ''}
                </p>
                {historyResult.kept_running.length > 0 && (
                  <p>
                    Still installing, kept running:{' '}
                    {historyResult.kept_running
                      .map((row) => `${row.zone_id} · ${row.connector || row.connector_id || 'device'} (${humanize(row.state).toLowerCase()})`)
                      .join('; ')}
                  </p>
                )}
              </div>
              <button className="button text-button" onClick={() => setHistoryResult(null)}>
                Dismiss
              </button>
            </div>
          )}
          <div className="firmware-campaign-toolbar">
            <label className="search-field">
              <span className="sr-only">Search firmware campaigns</span>
              <Icon name="search" />
              <input
                value={campaignQuery}
                onChange={(event) => setCampaignQuery(event.target.value)}
                placeholder="Search campaign, zone, release, or operator"
              />
            </label>
            <label>
              <span className="sr-only">Status</span>
              <select
                value={campaignStatus}
                onChange={(event) => setCampaignStatus(event.target.value)}
              >
                <option value="">All statuses</option>
                <option value="ACTIVE">Active</option>
                <option value="PAUSED">Paused</option>
                <option value="COMPLETED">Completed</option>
                <option value="CANCELLED">Cancelled</option>
              </select>
            </label>
            <label>
              <span className="sr-only">Zone</span>
              <select
                value={campaignZone}
                onChange={(event) => setCampaignZone(event.target.value)}
              >
                <option value="">All zones</option>
                {zones.map((zone) => (
                  <option key={zone}>{zone}</option>
                ))}
              </select>
            </label>
            <label>
              <span className="sr-only">Release</span>
              <select
                value={campaignRelease}
                onChange={(event) => setCampaignRelease(event.target.value)}
              >
                <option value="">All releases</option>
                {catalogReleases.map((release) => (
                  <option key={release.release_id} value={release.release_id}>
                    {firmwareName(release)}
                  </option>
                ))}
              </select>
            </label>
            {campaignFilterCount > 0 && (
              <button
                className="button secondary"
                onClick={() => {
                  setCampaignQuery('')
                  setCampaignStatus('')
                  setCampaignZone('')
                  setCampaignRelease('')
                }}
              >
                Clear filters
              </button>
            )}
          </div>
          {campaignError && (
            <div className="firmware-inline-error pattern-blocked" role="alert">
              <Icon name="alert" />
              <div>
                <strong>Latest campaign history could not be loaded</strong>
                <p>
                  {campaignError}
                  {campaignRows.length
                    ? ' Existing results remain visible.'
                    : ''}
                </p>
              </div>
              <button
                className="button secondary"
                onClick={() => void loadCampaigns()}
              >
                Retry
              </button>
            </div>
          )}
          {campaignLoading && !campaignRows.length && (
            <div className="firmware-skeleton">
              <i />
              <i />
              <i />
            </div>
          )}
          <div className="firmware-campaign-list">
            {campaignRows.map((campaign) => {
              const inProgress = sumStates(
                campaign.counts,
                activeDeploymentStates,
              )
              const succeeded =
                campaign.counts.SUCCEEDED || campaign.counts.succeeded || 0
              const attention = sumStates(
                campaign.counts,
                attentionDeploymentStates,
              )
              const progress = campaign.eligible
                ? Math.min(
                    100,
                    Math.round((succeeded / campaign.eligible) * 100),
                  )
                : 0
              return (
                <article
                  key={campaign.campaign_id}
                  className={`firmware-campaign-card pattern-${statusPattern(campaign.status)}`}
                >
                  <header>
                    <span>
                      <p className="eyebrow">
                        {campaign.zone_name || campaign.zone_id}
                      </p>
                      <h3>Zone Lite {campaign.version || 'unknown'}</h3>
                      <small>{campaign.campaign_id}</small>
                    </span>
                    <span className="firmware-campaign-badges">
                      <StatusBadge state={campaign.status} />
                      {campaign.archived_at && <StatusBadge state="ARCHIVED" />}
                    </span>
                  </header>
                  <div className="firmware-campaign-progress">
                    <div>
                      <strong>{progress}% complete</strong>
                      <span>
                        {succeeded} succeeded · {inProgress} active ·{' '}
                        {attention} attention
                      </span>
                    </div>
                    <div
                      role="progressbar"
                      aria-label={`Campaign progress for ${campaign.zone_name || campaign.zone_id}`}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={progress}
                    >
                      <i style={{ width: `${progress}%` }} />
                    </div>
                  </div>
                  <dl>
                    <div>
                      <dt>Eligible scope</dt>
                      <dd>{campaign.eligible}</dd>
                    </div>
                    <div>
                      <dt>Safely excluded</dt>
                      <dd>{campaign.legacy_skipped}</dd>
                    </div>
                    <div>
                      <dt>Operator</dt>
                      <dd>{campaign.actor}</dd>
                    </div>
                    <div>
                      <dt>Last update</dt>
                      <dd>{relativeTime(campaign.updated_at)}</dd>
                    </div>
                  </dl>
                  {campaign.status === 'ACTIVE' && campaign.waiting && (
                    <div className="firmware-campaign-waiting">
                      <Icon name="clock" />
                      <span>
                        <strong>{waitingLabel(campaign.waiting.code)}</strong>
                        {[campaign.waiting.display_name || campaign.waiting.connector_id, campaign.waiting.message]
                          .filter(Boolean)
                          .join(' · ')}
                      </span>
                    </div>
                  )}
                  {campaign.pause_reason && liveCampaignStates.includes(campaign.status) && (
                    <div className="firmware-campaign-alert">
                      <Icon name="alert" />
                      <span>
                        <strong>Rollout paused</strong>
                        {campaign.pause_reason}
                      </span>
                    </div>
                  )}
                  <footer>
                    <button
                      className="button secondary"
                      onClick={() => setCampaignDrawer(campaign)}
                    >
                      <Icon name="search" /> Inspect deployments
                    </button>
                    {campaign.status === 'ACTIVE' && (
                      <button
                        className="button secondary"
                        disabled={acting !== null}
                        onClick={() => void controlCampaign(campaign, 'pause')}
                      >
                        <Icon name="pause" /> Pause
                      </button>
                    )}
                    {campaign.status === 'PAUSED' && (
                      <button
                        className="button primary"
                        disabled={acting !== null || campaign.release_state === 'REVOKED'}
                        title={
                          campaign.release_state === 'REVOKED'
                            ? 'A revoked release cannot resume.'
                            : undefined
                        }
                        onClick={() => void controlCampaign(campaign, 'resume')}
                      >
                        <Icon name="refresh" /> Resume
                      </button>
                    )}
                    {liveCampaignStates.includes(campaign.status) && (
                      <button
                        className="button destructive-outline"
                        disabled={acting !== null}
                        aria-label={`Cancel campaign for ${campaign.zone_name || campaign.zone_id}`}
                        onClick={() => void controlCampaign(campaign, 'cancel')}
                      >
                        <Icon name="x" /> Cancel
                      </button>
                    )}
                    {closedCampaignStates.includes(campaign.status) && !campaign.archived_at && (
                      <button
                        className="button secondary"
                        disabled={acting !== null}
                        aria-label={`Archive campaign for ${campaign.zone_name || campaign.zone_id}`}
                        onClick={() => void archiveCampaign(campaign)}
                      >
                        <Icon name="download" /> Archive
                      </button>
                    )}
                  </footer>
                </article>
              )
            })}
            {!campaignLoading && !campaignRows.length && (
              <div className="empty-state">
                <Icon name="shield" />
                <h3>
                  {campaignFilterCount
                    ? 'No campaigns match these filters.'
                    : 'No firmware campaigns have been started.'}
                </h3>
                <p>
                  Use Update firmware to start one-click campaigns from an
                  enabled signed release.
                </p>
              </div>
            )}
          </div>
          {campaignCursor && (
            <div className="load-more">
              <button
                className="button secondary"
                disabled={campaignLoadingMore}
                onClick={() =>
                  void loadCampaigns({
                    cursor: campaignCursor,
                    append: true,
                    quiet: true,
                  })
                }
              >
                {campaignLoadingMore
                  ? 'Loading older campaigns…'
                  : 'Load older campaigns'}
              </button>
              <small>
                {campaignRows.length.toLocaleString()} of{' '}
                {campaignFilteredTotal.toLocaleString()} matching campaigns
                loaded
              </small>
            </div>
          )}
        </section>
      )}
      {updateOpen && (
        <UpdateFirmwareDialog
          releases={catalogReleases}
          devices={devices}
          campaigns={campaignRows}
          enabled={enabled}
          hilEnabled={hilEnabled}
          showOlder={showOlderReleases}
          onClose={() => setUpdateOpen(false)}
          onDone={async () => {
            await Promise.all([loadCatalog(), loadCampaigns({ quiet: true })])
          }}
          toast={toast}
        />
      )}
      {campaignDrawer && (
        <CampaignDetailDrawer
          seed={campaignDrawer}
          onClose={() => setCampaignDrawer(null)}
          onControl={controlCampaign}
          onArchive={archiveCampaign}
          acting={acting !== null}
          toast={toast}
        />
      )}
      {revokeRelease && (
        <Dialog
          titleId="revoke-firmware-title"
          title={`Revoke ${firmwareName(revokeRelease)}`}
          description="Revocation blocks new offers and pauses active campaigns for these exact signed bytes."
          onClose={() => {
            setRevokeRelease(null)
            setRevokeReason('')
            setRevokePassword('')
            setRevokeError('')
          }}
        >
          <div className="dialog-body">
            <div className="destructive-copy pattern-blocked">
              <Icon name="alert" />
              <div>
                <h3>This release will no longer be deployable.</h3>
                <p>
                  Devices already applying signed bytes continue according to
                  their durable safety state and require explicit review.
                </p>
              </div>
            </div>
            <label>
              Audited revocation reason
              <textarea
                value={revokeReason}
                onChange={(event) => setRevokeReason(event.target.value)}
                maxLength={200}
                rows={3}
              />
            </label>
            <label>
              Administrator password
              <input
                type="password"
                autoComplete="current-password"
                value={revokePassword}
                onChange={(event) => setRevokePassword(event.target.value)}
              />
            </label>
            {revokeError && (
              <div className="message pattern-blocked" role="alert">
                <Icon name="alert" />
                {revokeError}
              </div>
            )}
            <div className="dialog-actions">
              <button
                className="button secondary"
                onClick={() => setRevokeRelease(null)}
              >
                Keep release available
              </button>
              <button
                className="button destructive"
                disabled={
                  revokeBusy ||
                  revokeReason.trim().length < 10 ||
                  !revokePassword
                }
                onClick={() => void revoke()}
              >
                {revokeBusy ? 'Revoking…' : 'Revoke signed release'}
              </button>
            </div>
          </div>
        </Dialog>
      )}
    </div>
  )
}
