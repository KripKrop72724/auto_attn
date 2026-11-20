// The fleet page: key figures and the device list or map (moved from App.tsx).
// Home and Devices both show it until their own pages replace it.
import { Suspense, lazy, useState } from 'react'
import { Icon } from '../Icon'
import { deviceActivity } from '../hikvisionHealth'
import { relativeTime } from '../lib/time'
import { firmwareLabel, humanizeStatus, isOnlineState, statusPattern, terminalLinkNeedsAttention } from '../status'
import type { Device, Overview } from '../types'
import { Metric, availabilityTone, metricsTime } from '../ui/Metric'
import { PageHeader } from '../ui/PageHeader'
import { StatusBadge, TerminalLinkBadge, showsTerminalLinkBadge } from '../ui/StatusChip'
import { autoReleaseHeadline, useUnknownAutoReleaseStatus } from './unknownAutoRelease'
import { healthReasonLine, healthTierPattern, primaryHealthReason } from '../words/health'

const FleetMap = lazy(() => import('./FleetMap').then((module) => ({ default: module.FleetMap })))

// The owner's automatic release of unknown-identity punches, at a glance. It
// polls its own small status (saved counts only) and hides for non-admins.
function UnknownReleaseMetric({ onOpen }: { onOpen: () => void }) {
  const { data } = useUnknownAutoReleaseStatus(true)
  if (!data) return null
  const headline = autoReleaseHeadline(data)
  return <Metric label="Unknown-identity release" value={headline.value} detail={headline.detail} icon="refresh" tone={headline.tone} onClick={onOpen} />
}

export function FleetView({
  devices,
  overview,
  loading,
  onInspect,
  onManageUsers,
  onNavigateAlerts,
  onNavigateReconciliation,
  onNavigateUnknownRelease,
}: {
  devices: Device[]
  /** Null until the first overview answer: its tiles show "Loading…", never 0. */
  overview: Overview | null
  loading: boolean
  onInspect: (device: Device) => void
  onManageUsers: (device: Device) => void
  onNavigateAlerts: () => void
  onNavigateReconciliation: () => void
  onNavigateUnknownRelease: () => void
}) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('ALL')
  const [sort, setSort] = useState<'name' | 'last-contact' | 'state'>('last-contact')
  const [mode, setMode] = useState<'map' | 'list'>('map')
  const [inventory, setInventory] = useState<'fleet' | 'spares'>('fleet')
  const activeDevices = devices.filter((device) => !device.is_spare)
  const spareDevices = devices.filter((device) => device.is_spare)
  const inventoryDevices = inventory === 'fleet' ? activeDevices : spareDevices
  const needle = query.trim().toLowerCase()
  // Online with warnings is still online. A terminal link that is down needs
  // attention even while the ESP itself is online.
  const terminalDown = (device: Device) => terminalLinkNeedsAttention(device.terminal_link?.state)
  const matchesState = (device: Device) => inventory === 'spares' || filter === 'ALL'
    || (filter === 'ATTENTION' ? !isOnlineState(device.state) || terminalDown(device)
      : filter === 'TERMINAL_LINK_DOWN' ? terminalDown(device)
        : device.state === filter)
  const shown = inventoryDevices.filter(
    (device) =>
      matchesState(device) &&
      `${device.display_name} ${device.zone_name} ${device.zone_id} ${device.hardware_id} ${device.zkt?.serial || ''}`
        .toLowerCase()
        .includes(needle),
  ).sort((left, right) => sort === 'name'
    ? left.display_name.localeCompare(right.display_name)
    : sort === 'state'
      ? left.state.localeCompare(right.state) || left.display_name.localeCompare(right.display_name)
      : +new Date(right.last_seen_at || 0) - +new Date(left.last_seen_at || 0))
  const counts = overview || { total: 0, open_alerts: 0, active_leases: 0 }
  const countsLoading = !overview
  const withWarnings = counts.online_with_warnings || 0
  const online = (counts.online || 0) + withWarnings
  const availability = counts.total ? Math.round((online / counts.total) * 100) : 0
  const attention =
    (counts.offline || 0) +
    (counts.degraded || 0) +
    (counts.flapping || 0) +
    (counts.quarantined_duplicate_serial || 0)
  const terminalAttention = counts.terminal_attention || 0
  // Acknowledged alerts stay OPEN; the queue counts only those still needing action.
  const needsAction = counts.open_unacknowledged_alerts ?? counts.open_alerts
  // The quick overview (`sections=fleet`) leaves out the delivery and recovery
  // figures; until the full answer arrives those tiles load instead of showing 0.
  const delivery = counts.ords_delivery
  const deliveryLoading = countsLoading || !delivery
  const recovery = counts.attendance_recovery?.delivery
  const recoveryLoading = countsLoading || !counts.attendance_recovery
  const backlogStale = Boolean(delivery?.oldest_backlog_at && Date.now() - +new Date(delivery.oldest_backlog_at) > 15 * 60_000)
  const metricsAsOf = overview ? metricsTime(overview) : null
  const filtered = Boolean(needle) || (inventory === 'fleet' && filter !== 'ALL')
  const clearFilters = () => { setQuery(''); setFilter('ALL') }
  const showAttention = () => { setInventory('fleet'); setFilter('ATTENTION'); setMode('list') }
  return (
    <>
      <PageHeader
        title={inventory === 'fleet' ? 'Fleet' : 'Spare device inventory'}
        description={inventory === 'fleet'
          ? 'Live health of every Zone Lite connector and its attendance terminal.'
          : 'Backup devices, kept apart from fleet health, outages, and the operations queue.'}
        action={inventory === 'fleet' && metricsAsOf
          ? <span className="fleet-toolbar-note" title="Time the server computed these fleet metrics"><Icon name="clock" /> Metrics as of {metricsAsOf}</span>
          : undefined}
      />
      <div className="section-tabs fleet-inventory-tabs" role="tablist" aria-label="Device inventory">
        <button role="tab" aria-selected={inventory === 'fleet'} className={inventory === 'fleet' ? 'active' : ''} onClick={() => setInventory('fleet')}><Icon name="grid" /> Active fleet <span className="tab-count">{activeDevices.length}</span></button>
        <button role="tab" aria-selected={inventory === 'spares'} className={inventory === 'spares' ? 'active' : ''} onClick={() => setInventory('spares')}><Icon name="server" /> Spares <span className="tab-count">{spareDevices.length}</span></button>
      </div>
      {inventory === 'fleet' ? <section className="metric-grid" aria-label="Fleet key indicators">
        <Metric label="Fleet availability" loading={countsLoading} value={`${availability}%`} detail={`${online} of ${counts.total} online${withWarnings ? ` (${withWarnings} with warnings)` : ''}`} icon="pulse" tone={availabilityTone(availability, counts.total)} onClick={attention || terminalAttention || online < counts.total ? showAttention : undefined} />
        <Metric label="Operations queue" loading={countsLoading} value={needsAction} detail={`${attention} device${attention === 1 ? '' : 's'} degraded or offline${terminalAttention ? ` · ${terminalAttention} terminal link${terminalAttention === 1 ? '' : 's'} down` : ''}`} icon="alert" tone={needsAction ? 'warning' : 'neutral'} onClick={onNavigateAlerts} />
        <Metric
          label="ORDS delivery queue"
          loading={deliveryLoading}
          value={delivery?.backlog ?? 0}
          detail={`${delivery?.retrying ?? 0} retrying · ${delivery?.blocked_identity ?? 0} identity blocked · ${delivery?.quarantined ?? 0} quarantined`}
          icon="clock"
          tone={(delivery?.retrying ?? 0) > 0 ? 'critical' : (delivery?.backlog ?? 0) > 0 && backlogStale ? 'warning' : 'neutral'}
        />
        <Metric
          label="Attendance recovery"
          loading={recoveryLoading}
          value={recovery?.safe_retryable ?? 0}
          detail={`${recovery?.identity_held ?? 0} held for identity · ${recovery?.permanent_review ?? 0} for review`}
          icon="refresh"
          tone={(recovery?.safe_retryable ?? 0) > 0 ? 'warning' : 'neutral'}
          onClick={onNavigateReconciliation}
        />
        <UnknownReleaseMetric onOpen={onNavigateUnknownRelease} />
        <Metric label="Enrollment leases" loading={countsLoading} value={counts.active_leases} detail="Temporary administrator access" icon="shield" />
      </section> : <section className="spare-inventory-banner" aria-label="Spare inventory monitoring policy">
        <span className="spare-inventory-icon"><Icon name="shield" /></span>
        <div><h2>Ready when the active fleet needs a replacement</h2><p>Spare devices keep their telemetry and management access, but their offline state and alerts do not affect fleet availability or the operations queue.</p></div>
        <div className="spare-inventory-count"><strong>{spareDevices.length}</strong><span>Spare device{spareDevices.length === 1 ? '' : 's'}</span></div>
      </section>}
      <section className="panel">
        <header className="panel-header">
          <div>
            <h2>{inventory === 'fleet' ? 'Devices' : 'Spare devices'}</h2>
            <p>{filtered ? `${shown.length} of ${inventoryDevices.length} shown` : inventory === 'fleet' ? `${inventoryDevices.length} connector${inventoryDevices.length === 1 ? '' : 's'} · open a device for live logs and controls` : 'Open a device to return it to active service.'}</p>
          </div>
          <div className="fleet-panel-actions">
            {inventory === 'fleet' && <span className="fleet-toolbar-note"><Icon name="shield" /> Secure auto-onboarding enabled</span>}
            {inventory === 'fleet' && <div className="segmented-control fleet-view-toggle" role="group" aria-label="Fleet view">
              <button type="button" className={mode === 'map' ? 'active' : ''} aria-pressed={mode === 'map'} onClick={() => setMode('map')}><Icon name="map" /> Map</button>
              <button type="button" className={mode === 'list' ? 'active' : ''} aria-pressed={mode === 'list'} onClick={() => setMode('list')}><Icon name="list" /> List</button>
            </div>}
          </div>
        </header>
        <div className="toolbar">
          <label className="search-field">
            <span className="sr-only">{inventory === 'fleet' ? 'Search active fleet' : 'Search spare devices'}</span>
            <Icon name="search" />
            <input
              type="search"
              placeholder={inventory === 'fleet' ? 'Search name, zone, MAC, or serial' : 'Search spare devices'}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          {inventory === 'fleet' && <label>
            <span className="sr-only">Filter by state</span>
            <select value={filter} onChange={(event) => setFilter(event.target.value)}>
              <option value="ALL">All states</option>
              <option value="ATTENTION">Needs attention</option>
              <option value="ONLINE">Online</option>
              <option value="ONLINE_WITH_WARNINGS">Online with warnings</option>
              <option value="DEGRADED">Degraded</option>
              <option value="OFFLINE">Offline</option>
              <option value="ONBOARDING">Onboarding</option>
              <option value="QUARANTINED_DUPLICATE_SERIAL">Quarantined</option>
              <option value="TERMINAL_LINK_DOWN">Terminal link down</option>
            </select>
          </label>}
          <label><span className="sr-only">Sort fleet</span><select value={sort} onChange={(event) => setSort(event.target.value as typeof sort)}><option value="last-contact">Newest contact first</option><option value="name">Device name</option><option value="state">Operational state</option></select></label>
        </div>
        {inventory === 'fleet' && mode === 'map' ? (
          <Suspense fallback={<div className="fleet-map-fallback" role="status"><Icon name="refresh" /> Preparing national map…</div>}>
            <FleetMap
              devices={shown}
              loading={loading && !devices.length}
              onInspect={onInspect}
              onManageUsers={onManageUsers}
              formatRelativeTime={relativeTime}
            />
          </Suspense>
        ) : (
          <div className="device-list" aria-busy={loading}>
            {loading && !shown.length && <div className="empty-state is-loading" role="status"><Icon name="refresh" /><h3>Loading live fleet…</h3></div>}
            {shown.length > 0 && <div className="device-list-head" aria-hidden="true"><span>Device</span><span>Terminal</span><span>Activity</span><span>Firmware</span><span>Status</span></div>}
            {shown.map((device) => {
              const primary = !device.is_spare && device.health?.mode === 'ENFORCED' ? primaryHealthReason(device.health) : null
              return <article className={`device-card ${device.is_spare ? 'spare-device-card pattern-notice' : `pattern-${statusPattern(device.state)}`}`} key={device.connector_id}>
                <button className="device-card-main" onClick={() => onInspect(device)} aria-label={`Inspect ${device.display_name}`}>
                  <span className="device-identity"><strong>{device.display_name}</strong><small>{device.zone_id} · {device.hardware_id}</small></span>
                  <span className="device-terminal"><strong>{device.zkt?.model || (device.firmware_family === 'hikvision' ? 'Hikvision terminal' : 'Awaiting terminal')}</strong><small>{[device.zkt?.ip_address, device.zkt?.serial].filter(Boolean).join(' · ') || 'Identity pending'}</small></span>
                  <span className="device-activity"><strong>{device.is_spare ? 'Reserve inventory' : deviceActivity(device)}</strong><small>{relativeTime(device.last_seen_at)}</small></span>
                  <span className="device-firmware"><strong>{device.firmware_version ? firmwareLabel(device.firmware_version) : 'Firmware unknown'}</strong><small>{device.ota_capable ? humanizeStatus(device.ota_state || 'OTA_READY') : 'Manual updates'}</small></span>
                  <span className="device-status-stack">
                    <StatusBadge state={device.is_spare ? 'SPARE' : device.state} live={!device.is_spare && device.connected} />
                    {!device.is_spare && showsTerminalLinkBadge(device.terminal_link) && <TerminalLinkBadge link={device.terminal_link} />}
                  </span>
                  <Icon name="chevron" />
                  {primary && <span className={`device-health-line pattern-${healthTierPattern(primary.tier)}`}>{healthReasonLine(primary)}</span>}
                </button>
                <div className="device-card-actions">
                  <button className="icon-button" onClick={() => onManageUsers(device)} aria-label={`Manage users on ${device.display_name}`} title="Manage users"><Icon name="users" /></button>
                </div>
              </article>
            })}
            {!loading && !shown.length && (
              <div className="empty-state">
                <Icon name={filtered ? 'search' : 'server'} />
                <h3>{inventoryDevices.length ? 'No devices match these filters' : inventory === 'spares' ? 'No spare devices yet' : 'Waiting for the first device'}</h3>
                <p>{inventoryDevices.length ? 'Try a different search or state.' : inventory === 'spares' ? 'Open any active device and move it to spare inventory when it is taken out of service.' : 'A securely flashed Zone Lite ESP appears here automatically after signed onboarding.'}</p>
                {filtered && inventoryDevices.length > 0 && <button type="button" className="button secondary small" onClick={clearFilters}>Clear filters</button>}
              </div>
            )}
          </div>
        )}
      </section>
    </>
  )
}
