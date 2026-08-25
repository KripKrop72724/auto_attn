import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from 'react'
import { z } from 'zod'
import './Firmware.css'
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
import { AnchoredLayer } from '../AnchoredLayer'
import type {
  Device,
  FirmwareCampaign,
  FirmwareCampaignTotals,
  FirmwareListResponse,
  FirmwareRelease,
  FirmwareReleaseTotals,
  FirmwareScopePreview,
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

const firmwareScopeSchema = z.object({
  scope_token: z.string().min(32),
  expires_at: z.string().min(1),
  release: z.object({
    release_id: z.string(),
    version: z.string(),
    state: z.string(),
  }),
  zone_id: z.string(),
  counts: z.object({
    candidates: z.number().int().nonnegative(),
    eligible: z.number().int().nonnegative(),
    excluded: z.number().int().nonnegative(),
    offline: z.number().int().nonnegative(),
  }),
  eligible: z.array(
    z
      .object({
        connector_id: z.string(),
        display_name: z.string(),
        zone_id: z.string(),
        hardware_id: z.string(),
        connected: z.boolean(),
      })
      .passthrough(),
  ),
  excluded: z.array(
    z
      .object({
        connector_id: z.string(),
        display_name: z.string(),
        zone_id: z.string(),
        hardware_id: z.string(),
        connected: z.boolean(),
        reason: z.string(),
      })
      .passthrough(),
  ),
})
const firmwareCampaignCreatedSchema = z.object({
  campaign_id: z.string().min(1),
  status: z.string(),
  eligible: z.number().int().nonnegative(),
  legacy_skipped: z.number().int().nonnegative(),
})

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

function FirmwareTabs({
  section,
  releases,
  campaigns,
  onChange,
}: {
  section: FirmwareSection
  releases: number
  campaigns: number
  onChange: (section: FirmwareSection) => void
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([])
  const tabs: Array<{ id: FirmwareSection; label: string; count?: number }> = [
    { id: 'overview', label: 'Overview' },
    { id: 'prepare', label: 'Prepare device' },
    { id: 'releases', label: 'Signed releases', count: releases },
    { id: 'campaigns', label: 'Campaigns', count: campaigns },
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
      className="firmware-workspace-tabs"
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
          {tab.count !== undefined && (
            <strong>{tab.count.toLocaleString()}</strong>
          )}
        </button>
      ))}
    </div>
  )
}

function CampaignControlDialog({
  campaign,
  action,
  onClose,
  onChanged,
  toast,
}: {
  campaign: FirmwareCampaign
  action: CampaignAction
  onClose: () => void
  onChanged: () => Promise<void>
  toast: Toast
}) {
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const control = async () => {
    setBusy(true)
    setError('')
    try {
      await api(
        `/api/v1/firmware/campaigns/${campaign.campaign_id}/${action}`,
        {
          method: 'POST',
          body: JSON.stringify({ reason: reason.trim(), password }),
        },
      )
      setPassword('')
      toast.notice(
        `Firmware campaign ${action === 'pause' ? 'paused' : action === 'resume' ? 'resumed' : 'cancelled'} with an audit entry.`,
      )
      await onChanged()
      onClose()
    } catch (reason) {
      setPassword('')
      setError(
        reason instanceof Error ? reason.message : 'Campaign control failed.',
      )
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog
      titleId="firmware-campaign-control-title"
      title={`${humanize(action)} firmware campaign`}
      description={`${campaign.zone_name || campaign.zone_id} · Zone Lite ${campaign.version || 'unknown'}`}
      onClose={onClose}
    >
      <div className="dialog-body">
        <div
          className={`info-copy pattern-${action === 'cancel' ? 'blocked' : 'waiting'}`}
        >
          <Icon name={action === 'cancel' ? 'alert' : 'shield'} />
          <div>
            <h3>
              {action === 'cancel'
                ? 'Cancellation cannot be resumed.'
                : 'The server verifies release and rollout safety before changing state.'}
            </h3>
            <p>
              Devices already applying signed bytes continue through their
              durable safety state. Audit evidence remains available.
            </p>
          </div>
        </div>
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
            Keep current state
          </button>
          <button
            className={`button ${action === 'cancel' ? 'destructive' : 'primary'}`}
            disabled={busy || reason.trim().length < 10 || !password}
            onClick={() => void control()}
          >
            {busy ? 'Verifying…' : `Confirm ${action}`}
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
}: {
  seed: FirmwareCampaign
  onClose: () => void
  onControl: (campaign: FirmwareCampaign, action: CampaignAction) => void
}) {
  const [campaign, setCampaign] = useState<FirmwareCampaign | null>(null)
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
                          <dt>Failure</dt>
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
            {['ACTIVE', 'PAUSED'].includes(row.status) && (
              <footer className="firmware-drawer-actions">
                {row.status === 'ACTIVE' ? (
                  <button
                    className="button secondary"
                    onClick={() => onControl(row, 'pause')}
                  >
                    <Icon name="pause" /> Pause rollout
                  </button>
                ) : (
                  <button
                    className="button primary"
                    disabled={row.release_state === 'REVOKED'}
                    title={
                      row.release_state === 'REVOKED'
                        ? 'A revoked release cannot resume.'
                        : undefined
                    }
                    onClick={() => onControl(row, 'resume')}
                  >
                    <Icon name="refresh" /> Resume rollout
                  </button>
                )}
                <button
                  className="button destructive"
                  onClick={() => onControl(row, 'cancel')}
                >
                  <Icon name="x" /> Cancel campaign
                </button>
              </footer>
            )}
          </>
        )}
      </div>
    </Dialog>
  )
}

function CampaignCreator({
  releases,
  devices,
  enabled,
  hilEnabled,
  onClose,
  onCreated,
  toast,
}: {
  releases: FirmwareRelease[]
  devices: Device[]
  enabled: boolean
  hilEnabled: boolean
  onClose: () => void
  onCreated: () => Promise<void>
  toast: Toast
}) {
  const [releaseId, setReleaseId] = useState('')
  const [zoneId, setZoneId] = useState('')
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [password, setPassword] = useState('')
  const [scope, setScope] = useState<FirmwareScopePreview | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [clock, setClock] = useState(() => Date.now())
  const selectedRelease =
    releases.find((release) => release.release_id === releaseId) || null
  const hilTarget = selectedRelease?.hil_target_mac
    ? devices.find(
        (device) =>
          device.hardware_id.toLowerCase() ===
          selectedRelease.hil_target_mac?.toLowerCase(),
      ) || null
    : null
  const isHil = selectedRelease?.state === 'HIL_ONLY'
  const zones = useMemo(
    () =>
      [
        ...new Map(
          devices.map((device) => [
            device.zone_id,
            device.zone_name || device.zone_id,
          ]),
        ).entries(),
      ].sort(([left], [right]) => left.localeCompare(right)),
    [devices],
  )
  const expiresIn = scope
    ? Math.max(0, Math.ceil((Date.parse(scope.expires_at) - clock) / 1000))
    : 0
  const expired = Boolean(scope && expiresIn <= 0)
  useEffect(() => {
    if (!scope) return
    const timer = window.setInterval(() => setClock(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [scope])
  const resetScope = () => {
    setScope(null)
    setConfirmation('')
    setPassword('')
  }
  const chooseRelease = (value: string) => {
    setReleaseId(value)
    resetScope()
    const next = releases.find((release) => release.release_id === value)
    const target = next?.hil_target_mac
      ? devices.find(
          (device) =>
            device.hardware_id.toLowerCase() ===
            next.hil_target_mac?.toLowerCase(),
        )
      : null
    setZoneId(next?.state === 'HIL_ONLY' ? target?.zone_id || '' : '')
  }
  const preview = async () => {
    if (!selectedRelease || !zoneId) return
    setPreviewBusy(true)
    setScope(null)
    setError('')
    try {
      setScope(
        firmwareScopeSchema.parse(
          await api('/api/v1/firmware/campaigns/preflight', {
            method: 'POST',
            body: JSON.stringify({
              release_id: selectedRelease.release_id,
              zone_id: zoneId,
            }),
          }),
        ) as FirmwareScopePreview,
      )
      setClock(Date.now())
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : 'Firmware scope could not be previewed.',
      )
    } finally {
      setPreviewBusy(false)
    }
  }
  const valid = Boolean(
    selectedRelease &&
      zoneId &&
      scope &&
      !expired &&
      scope.release.release_id === selectedRelease.release_id &&
      scope.zone_id === zoneId &&
      scope.counts.eligible > 0 &&
      reason.trim().length >= 10 &&
      confirmation === selectedRelease.version &&
      password &&
      !busy,
  )
  const start = async (event: FormEvent) => {
    event.preventDefault()
    if (!valid || !selectedRelease || !scope) return
    setBusy(true)
    setError('')
    try {
      const result = firmwareCampaignCreatedSchema.parse(
        await api('/api/v1/firmware/campaigns', {
          method: 'POST',
          body: JSON.stringify({
            release_id: selectedRelease.release_id,
            zone_id: zoneId,
            reason: reason.trim(),
            typed_confirmation: confirmation,
            password,
            scope_token: scope.scope_token,
            idempotency_key: idempotency('firmware-campaign'),
          }),
        }),
      )
      setPassword('')
      setConfirmation('')
      toast.notice(
        `Firmware campaign created for ${result.eligible} eligible device${result.eligible === 1 ? '' : 's'}; ${result.legacy_skipped} safely excluded.`,
      )
      await onCreated()
      onClose()
    } catch (reason) {
      setPassword('')
      setConfirmation('')
      setError(
        reason instanceof Error
          ? reason.message
          : 'Firmware campaign could not be created.',
      )
    } finally {
      setBusy(false)
    }
  }
  const stage = !releaseId ? 1 : !zoneId ? 2 : !scope || expired ? 3 : 4
  return (
    <Dialog
      titleId="firmware-campaign-create-title"
      title="Create a constrained firmware campaign"
      description="Select signed bytes, verify exact server scope, then complete administrator step-up."
      onClose={onClose}
      className="firmware-campaign-dialog"
    >
      <div className="firmware-campaign-wizard">
        <ol aria-label="Campaign setup progress">
          {['Release', 'Zone', 'Scope', 'Confirm'].map((label, index) => (
            <li
              key={label}
              className={
                index + 1 < stage ? 'done' : index + 1 === stage ? 'active' : ''
              }
              aria-current={index + 1 === stage ? 'step' : undefined}
            >
              <span>
                {index + 1 < stage ? <Icon name="check" /> : index + 1}
              </span>
              <strong>{label}</strong>
            </li>
          ))}
        </ol>
        <form onSubmit={(event) => void start(event)}>
          <section>
            <p className="eyebrow">1 · SIGNED RELEASE</p>
            <label>
              Release channel
              <select
                value={releaseId}
                onChange={(event) => chooseRelease(event.target.value)}
              >
                <option value="">Select signed bytes</option>
                {releases
                  .filter((release) => release.state !== 'REVOKED')
                  .map((release) => (
                    <option key={release.release_id} value={release.release_id}>
                      Zone Lite {release.version} ·{' '}
                      {release.state === 'HIL_ONLY'
                        ? 'HIL-only exact MAC'
                        : 'Production'}
                    </option>
                  ))}
              </select>
            </label>
            {selectedRelease && (
              <div className="firmware-wizard-context">
                <StatusBadge state={selectedRelease.state} />
                <span>
                  <strong>{selectedRelease.partition_layout}</strong>
                  <small>
                    Key {selectedRelease.signing_key_id} · published{' '}
                    {dateTime(selectedRelease.published_at)}
                  </small>
                </span>
              </div>
            )}
          </section>
          <section>
            <p className="eyebrow">2 · DESTINATION ZONE</p>
            <label>
              Zone
              <select
                value={zoneId}
                disabled={isHil}
                onChange={(event) => {
                  setZoneId(event.target.value)
                  resetScope()
                }}
              >
                <option value="">Select a zone</option>
                {zones.map(([id, name]) => (
                  <option key={id} value={id}>
                    {name} · {id}
                  </option>
                ))}
              </select>
            </label>
            {isHil && (
              <div
                className={`firmware-wizard-context pattern-${hilTarget ? 'confirmed' : 'blocked'}`}
              >
                <Icon name={hilTarget ? 'shield' : 'alert'} />
                <span>
                  <strong>Exact-MAC HIL quarantine</strong>
                  <small>
                    {selectedRelease?.hil_target_mac || 'No target MAC'}
                    {hilTarget
                      ? ` · ${hilTarget.display_name}`
                      : ' · no registered match'}
                  </small>
                </span>
              </div>
            )}
          </section>
          <section>
            <p className="eyebrow">3 · SERVER-AUTHORITATIVE SCOPE</p>
            {selectedRelease && zoneId && (!scope || expired) && (
              <button
                type="button"
                className="button secondary"
                disabled={previewBusy || (isHil ? !hilEnabled : !enabled)}
                onClick={() => void preview()}
              >
                <Icon name="search" />{' '}
                {previewBusy
                  ? 'Calculating exact scope…'
                  : expired
                    ? 'Refresh expired scope'
                    : 'Preview eligible and excluded devices'}
              </button>
            )}
            {scope && !expired && (
              <div className="firmware-scope-result" aria-live="polite">
                <header>
                  <span>
                    <strong>{scope.counts.eligible}</strong> eligible
                  </span>
                  <span>
                    <strong>{scope.counts.excluded}</strong> excluded
                  </span>
                  <span>
                    <strong>{scope.counts.offline}</strong> offline
                  </span>
                  <StatusBadge state={`${expiresIn}s REMAINING`} />
                </header>
                <div className="firmware-scope-columns">
                  <section>
                    <h3>Eligible devices</h3>
                    {scope.eligible.map((device) => (
                      <article key={device.connector_id}>
                        <Icon name="check" />
                        <span>
                          <strong>{device.display_name}</strong>
                          <small>
                            {device.hardware_id} ·{' '}
                            {device.connected
                              ? 'online'
                              : 'offline, remains pending'}
                          </small>
                        </span>
                      </article>
                    ))}
                  </section>
                  <section>
                    <h3>Safely excluded</h3>
                    {scope.excluded.map((device) => (
                      <article key={device.connector_id}>
                        <Icon name="alert" />
                        <span>
                          <strong>{device.display_name}</strong>
                          <small>
                            {humanize(device.reason)} · {device.hardware_id}
                          </small>
                        </span>
                      </article>
                    ))}
                  </section>
                </div>
                <button
                  type="button"
                  className="button text-button"
                  onClick={() => void preview()}
                >
                  <Icon name="refresh" /> Recalculate scope
                </button>
              </div>
            )}
          </section>
          <section>
            <p className="eyebrow">4 · AUDIT & CONFIRM</p>
            <label>
              Audited reason
              <textarea
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                maxLength={500}
                placeholder="Why is this rollout required? At least 10 characters."
              />
            </label>
            <label>
              Type exact version <code>{selectedRelease?.version || '—'}</code>
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
          </section>
          {error && (
            <div className="message pattern-blocked" role="alert">
              <Icon name="alert" />
              {error}
            </div>
          )}
          <footer>
            <button
              type="button"
              className="button secondary"
              onClick={onClose}
            >
              Cancel
            </button>
            <button className="button primary" disabled={!valid}>
              <Icon name="power" />{' '}
              {busy ? 'Starting audited campaign…' : 'Start sequential rollout'}
            </button>
          </footer>
        </form>
      </div>
    </Dialog>
  )
}

function FirmwareCancelMenu({ onCancel }: { onCancel: () => void }) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  return (
    <div className="firmware-action-menu">
      <button ref={triggerRef} className="button secondary" type="button" aria-label="More firmware campaign actions" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((value) => !value)}><Icon name="menu" /> More</button>
      {open && <AnchoredLayer anchorRef={triggerRef} className="firmware-action-layer" mobileSheet preferredWidth={310} onDismiss={(reason) => { setOpen(false); if (reason === 'escape') triggerRef.current?.focus() }}>
        <div className="firmware-action-panel" role="group" aria-label="Firmware campaign actions">
          <button type="button" className="danger-action" onClick={() => { setOpen(false); onCancel() }}><Icon name="x" /><span><strong>Cancel campaign</strong><small>Signed evidence and device state remain durable.</small></span></button>
        </div>
      </AnchoredLayer>}
    </div>
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
  const [creatorOpen, setCreatorOpen] = useState(false)
  const [campaignDrawer, setCampaignDrawer] = useState<FirmwareCampaign | null>(
    null,
  )
  const [campaignControl, setCampaignControl] = useState<{
    campaign: FirmwareCampaign
    action: CampaignAction
  } | null>(null)
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
          `/api/v1/firmware/releases${queryString({ q: releaseQuery.trim() || undefined, state: releaseState || undefined, cursor, limit: 24 })}`,
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
    [releaseQuery, releaseState],
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
          `/api/v1/firmware/campaigns${queryString({ view: 'summary', q: campaignQuery.trim() || undefined, status: campaignStatus || undefined, zone_id: campaignZone || undefined, release_id: campaignRelease || undefined, cursor, limit: 24 })}`,
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
    [campaignQuery, campaignStatus, campaignZone, campaignRelease],
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
        eyebrow="SIGNED FIRMWARE CONTROL PLANE"
        title="Firmware operations"
        description="Prepare Zone Lite hardware, verify signed release channels, and operate exact-scope sequential rollouts with durable evidence and audited controls."
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
              onClick={() => setCreatorOpen(true)}
            >
              <Icon name="plus" /> New campaign
            </button>
          </div>
        }
      />
      <section className="firmware-channel-strip">
        <article className={`pattern-${enabled ? 'confirmed' : 'blocked'}`}>
          <Icon name="shield" />
          <span>
            <strong>National production OTA</strong>
            <small>
              {enabled
                ? 'AVAILABLE releases may use exact zone scope'
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
      </section>
      <FirmwareTabs
        section={section}
        releases={releaseTotals.all}
        campaigns={campaignTotals.campaigns.all || 0}
        onChange={onSection}
      />
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
              detail="Authoritative OTA_READY device state"
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
                    ? `Zone Lite ${productionRelease.version}`
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
                  ? `Zone Lite ${hilRelease.version}`
                  : 'No active HIL candidate'}
              </h3>
              <p>
                {hilRelease
                  ? `Bound to ${hilRelease.hil_target_mac || 'an unresolved target MAC'}. It cannot be offered nationally.`
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
                    <small>deployments in progress</small>
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
                Compact cryptographic inventory with production, HIL, and
                revoked channels kept visually distinct.
              </p>
            </div>
            <StatusBadge
              state={`${releaseFilteredTotal.toLocaleString()} MATCHING`}
            />
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
              <span>Channel</span>
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
                Clear {releaseFilterCount}
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
                    <h3>Zone Lite {release.version}</h3>
                    <small>{release.release_id}</small>
                  </span>
                  <StatusBadge state={release.state} />
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
                    <strong>{release.image_size.toLocaleString()} bytes</strong>
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
                        {release.hil_target_mac || 'Target MAC is unavailable'}
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
                          <Icon name="list" />
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
                          <Icon name="list" />
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
                            <Icon name="list" />
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
                      className="button destructive"
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
                  ? 'Loading older releases…'
                  : 'Load older releases'}
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
                Attention-first sequential rollout history with details loaded
                only when inspected.
              </p>
            </div>
            <StatusBadge
              state={`${campaignFilteredTotal.toLocaleString()} MATCHING`}
            />
          </div>
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
              <span>Status</span>
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
              <span>Zone</span>
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
              <span>Release</span>
              <select
                value={campaignRelease}
                onChange={(event) => setCampaignRelease(event.target.value)}
              >
                <option value="">All releases</option>
                {catalogReleases.map((release) => (
                  <option key={release.release_id} value={release.release_id}>
                    Zone Lite {release.version}
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
                Clear {campaignFilterCount}
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
                    <StatusBadge state={campaign.status} />
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
                  {campaign.pause_reason && (
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
                        onClick={() =>
                          setCampaignControl({ campaign, action: 'pause' })
                        }
                      >
                        <Icon name="pause" /> Pause
                      </button>
                    )}
                    {campaign.status === 'PAUSED' && (
                      <button
                        className="button primary"
                        disabled={campaign.release_state === 'REVOKED'}
                        title={
                          campaign.release_state === 'REVOKED'
                            ? 'A revoked release cannot resume.'
                            : undefined
                        }
                        onClick={() =>
                          setCampaignControl({ campaign, action: 'resume' })
                        }
                      >
                        <Icon name="refresh" /> Resume
                      </button>
                    )}
                    {['ACTIVE', 'PAUSED'].includes(campaign.status) && (
                      <FirmwareCancelMenu onCancel={() => setCampaignControl({ campaign, action: 'cancel' })} />
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
                  Create a guarded exact-scope campaign from an enabled signed
                  release.
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
      {creatorOpen && (
        <CampaignCreator
          releases={catalogReleases}
          devices={devices}
          enabled={enabled}
          hilEnabled={hilEnabled}
          onClose={() => setCreatorOpen(false)}
          onCreated={async () => {
            await Promise.all([loadCatalog(), loadCampaigns({ quiet: true })])
          }}
          toast={toast}
        />
      )}
      {campaignDrawer && (
        <CampaignDetailDrawer
          seed={campaignDrawer}
          onClose={() => setCampaignDrawer(null)}
          onControl={(campaign, action) => {
            setCampaignDrawer(null)
            setCampaignControl({ campaign, action })
          }}
        />
      )}
      {campaignControl && (
        <CampaignControlDialog
          campaign={campaignControl.campaign}
          action={campaignControl.action}
          onClose={() => setCampaignControl(null)}
          onChanged={async () => {
            await loadCampaigns({ quiet: true })
          }}
          toast={toast}
        />
      )}
      {revokeRelease && (
        <Dialog
          titleId="revoke-firmware-title"
          title={`Revoke Zone Lite ${revokeRelease.version}`}
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
