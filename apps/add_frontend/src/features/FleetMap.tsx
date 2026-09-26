import { deviceActivity } from '../hikvisionHealth'
import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import pakistanMapUrl from '../assets/pakistan-operations-map.svg'
import { groupFleetLocations, type FleetLocationGroup, type FleetLocationId } from '../fleetLocations'
import { Icon } from '../Icon'
import { firmwareLabel, humanizeStatus, statusPattern, type StatusPattern } from '../status'
import type { Device } from '../types'
import './fleet-map.css'

export interface FleetMapProps {
  devices: Device[]
  loading: boolean
  onInspect: (device: Device) => void
  onManageUsers: (device: Device) => void
  formatRelativeTime: (value?: string | null) => string
}

const patternLabel: Record<StatusPattern, string> = {
  confirmed: 'All online',
  waiting: 'Needs attention',
  blocked: 'Critical attention',
  notice: 'Status pending',
}

const patternIcon: Record<StatusPattern, Parameters<typeof Icon>[0]['name']> = {
  confirmed: 'check',
  waiting: 'clock',
  blocked: 'alert',
  notice: 'info',
}

function MapStatus({ pattern, label }: { pattern: StatusPattern; label?: string }) {
  return <span className={`fleet-map-status pattern-${pattern}`}><Icon name={patternIcon[pattern]} />{label || patternLabel[pattern]}</span>
}

function DeviceRows({
  devices,
  onInspect,
  onManageUsers,
  formatRelativeTime,
}: Pick<FleetMapProps, 'devices' | 'onInspect' | 'onManageUsers' | 'formatRelativeTime'>) {
  return <div className="fleet-location-devices">
    {devices.map((device) => {
      const pattern = statusPattern(device.state)
      return <article className={`fleet-location-device pattern-${pattern}`} key={device.connector_id}>
        <button className="fleet-location-device-main" onClick={() => onInspect(device)} aria-label={`Inspect ${device.display_name}`}>
          <span className="fleet-location-device-copy">
            <strong>{device.display_name}</strong>
            <small>{device.zkt?.model || (device.firmware_family === 'hikvision' ? 'Hikvision terminal' : 'Awaiting terminal')} · {deviceActivity(device)}</small>
            <span>{device.zone_id} · {formatRelativeTime(device.last_seen_at)}</span>
          </span>
          <MapStatus pattern={pattern} label={humanizeStatus(device.state)} />
          <Icon name="chevron" />
        </button>
        <div className="fleet-location-device-actions">
          <button className="text-button" onClick={() => onManageUsers(device)}><Icon name="users" /> Manage users</button>
          <span>{[device.zkt?.serial, device.firmware_version && firmwareLabel(device.firmware_version)].filter(Boolean).join(' · ')}</span>
        </div>
      </article>
    })}
  </div>
}

function LocationSummary({ group, onSelect, selected = false }: { group: FleetLocationGroup; onSelect: () => void; selected?: boolean }) {
  return <button
    className={`fleet-map-location-summary pattern-${group.pattern} ${selected ? 'selected' : ''}`}
    onClick={onSelect}
    aria-label={`Open ${group.definition.city} location, ${group.total} device${group.total === 1 ? '' : 's'}`}
    aria-pressed={selected}
    title={`${group.definition.region} · ${patternLabel[group.pattern]}`}
  >
    <Icon name={patternIcon[group.pattern]} />
    <strong>{group.definition.city}</strong>
    <span>{group.total}</span>
  </button>
}

export function FleetMap({ devices, loading, onInspect, onManageUsers, formatRelativeTime }: FleetMapProps) {
  const { groups, unmapped } = useMemo(() => groupFleetLocations(devices), [devices])
  const [selectedId, setSelectedId] = useState<FleetLocationId | null>(null)
  const selected = groups.find((group) => group.definition.id === selectedId) || null

  useEffect(() => {
    if (selectedId && !groups.some((group) => group.definition.id === selectedId)) setSelectedId(null)
  }, [groups, selectedId])

  const attention = groups.reduce((sum, group) => sum + group.attention, 0)
  const nationalPattern: StatusPattern = attention ? 'waiting' : devices.length ? 'confirmed' : 'notice'
  const markerStyle = (group: FleetLocationGroup) => ({
    '--marker-x': `${group.definition.mapX}%`,
    '--marker-y': `${group.definition.mapY}%`,
  }) as CSSProperties

  return <section className={`fleet-map-layout ${selected ? 'has-selection' : ''}`} aria-label="Pakistan device network map" aria-busy={loading}>
    <div className="fleet-map-surface">
      <div className="fleet-map-canvas">
        <header className="fleet-map-overview">
          <h3>{groups.length} operating location{groups.length === 1 ? '' : 's'}</h3>
          <MapStatus pattern={nationalPattern} label={attention ? `${attention} need${attention === 1 ? 's' : ''} attention` : devices.length ? 'Network healthy' : 'Awaiting devices'} />
        </header>
        <div className="fleet-map-stage">
          <img src={pakistanMapUrl} alt="" aria-hidden="true" />
          {groups.map((group) => {
            const selectedMarker = selectedId === group.definition.id
            return <button
              key={group.definition.id}
              className={`fleet-map-marker location-${group.definition.id} label-${group.definition.labelSide} pattern-${group.pattern} ${selectedMarker ? 'selected' : ''}`}
              style={markerStyle(group)}
              onClick={() => setSelectedId(group.definition.id)}
              aria-label={`${group.definition.city}, ${group.total} device${group.total === 1 ? '' : 's'}, ${patternLabel[group.pattern]}`}
              aria-pressed={selectedMarker}
              aria-controls={selectedMarker ? 'fleet-location-panel' : undefined}
            >
              <span className="fleet-map-marker-ripple" aria-hidden="true" />
              <span className="fleet-map-marker-core" aria-hidden="true"><i /><strong>{group.total}</strong></span>
              <span className="fleet-map-marker-label" aria-hidden="true"><strong>{group.definition.city}</strong><small>{patternLabel[group.pattern]}</small></span>
            </button>
          })}
        </div>

        {loading && <div className="fleet-map-loading" role="status"><Icon name="refresh" /> Synchronizing national fleet…</div>}
        {!loading && !groups.length && <div className="fleet-map-loading"><Icon name="map" /> No mapped devices match this view.</div>}

        {selected && <aside className="fleet-location-sheet" id="fleet-location-panel" key={selected.definition.id} aria-live="polite">
          <header className="fleet-location-sheet-head">
            <div>
              <h3>{selected.definition.city}</h3>
              <span>{selected.definition.region} · latest contact {formatRelativeTime(selected.lastSeenAt)}</span>
            </div>
            <button className="icon-button" onClick={() => setSelectedId(null)} aria-label={`Close ${selected.definition.city} details`}><Icon name="x" /></button>
          </header>
          <div className="fleet-location-metrics">
            <span><strong>{selected.total}</strong><small>Devices</small></span>
            <span><strong>{selected.online}</strong><small>Online</small></span>
            <span className={selected.attention ? 'needs-attention' : ''}><strong>{selected.attention}</strong><small>Attention</small></span>
          </div>
          <DeviceRows devices={selected.devices} onInspect={onInspect} onManageUsers={onManageUsers} formatRelativeTime={formatRelativeTime} />
        </aside>}

        <footer className="fleet-map-legend" aria-label="Map health legend">
          <span className="pattern-confirmed"><i />Online</span>
          <span className="pattern-waiting"><i />Attention</span>
          <span className="pattern-blocked"><i />Critical</span>
        </footer>
      </div>

      <nav className="fleet-map-location-index" aria-label="Mapped location index">
        <div className="fleet-map-location-list">
          {groups.map((group) => <LocationSummary key={group.definition.id} group={group} onSelect={() => setSelectedId(group.definition.id)} selected={selectedId === group.definition.id} />)}
        </div>
      </nav>
    </div>

    {unmapped.length > 0 && <section className="fleet-unmapped">
      <header><Icon name="alert" /><div><strong>Location not mapped</strong><span>{unmapped.length} device{unmapped.length === 1 ? '' : 's'} remain fully available.</span></div></header>
      <DeviceRows devices={unmapped} onInspect={onInspect} onManageUsers={onManageUsers} formatRelativeTime={formatRelativeTime} />
    </section>}
  </section>
}
