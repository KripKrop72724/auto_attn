// Software updates › Overview (08-advanced 2.2): Life and Health panels with the
// devices that are not up to date, the updates running now and the newest
// version per machine brand. Signing keys and test plans sit in Technical details.
import { useMemo, useState } from 'react'
import { deviceDivision, type DivisionKey } from '../../lib/division'
import { flag, oneOf, useUrlState } from '../../lib/urlState'
import type { DivisionChoice } from '../../lib/prefs'
import type { Device, UpdatesSection } from '../../types'
import { Button, DivisionTag, ErrorState, FilterBar, Skeleton, StatusChip, TechnicalDetails } from '../../ui'
import { Icon } from '../../Icon'
import {
  FAMILY_NAMES, SOFTWARE_PAGE, plainVersion, SOFTWARE_RANK, SOFTWARE_SHOW, newestVersion, softwareShowFor, softwareStatus,
  updateChip, updateStepWord, downloadPercent, type SoftwareContext, type SoftwareShow, type SoftwareStatus,
} from '../../words/software'
import { devicesOnVersion, updatePlace, type NameIndex, type UpdateRow } from './model'
import type { Catalog, RetryTarget } from './UpdatesPage'

const SHOW_VALUES = ['notcurrent', 'all', 'attention', 'updating', 'newer', 'current', 'technician'] as const
const overviewSchema = { show: oneOf(SHOW_VALUES, 'notcurrent'), spares: flag() }

type Row = { device: Device; status: SoftwareStatus; name: string; division: DivisionKey }

export function OverviewTab({
  devices, division, context, names, catalog, catalogError, live, testPlan,
  onRetryCatalog, onOpenUpdate, onRetry, onOpenDevice, onGo, onSetup,
}: {
  devices: Device[]
  division: DivisionChoice
  context: SoftwareContext
  names: NameIndex
  catalog: Catalog | null
  catalogError: boolean
  live: UpdateRow[] | null
  testPlan: string[]
  onRetryCatalog: () => Promise<void>
  onOpenUpdate: (row: UpdateRow | string) => void
  onRetry: (target: RetryTarget) => void
  onOpenDevice: (device: Device) => void
  onGo: (tab: UpdatesSection, extra?: Record<string, string>) => void
  onSetup: () => void
}) {
  const [filters, setFilters] = useUrlState(overviewSchema)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const loading = !context.devicesLoaded

  const rows: Row[] = useMemo(() => devices
    .filter((device) => filters.spares || !device.is_spare)
    .map((device) => {
      const named = names.device(device.connector_id)
      return { device, status: softwareStatus(device, context), name: named?.name ?? device.display_name, division: deviceDivision(device) }
    })
    .sort((a, b) => (SOFTWARE_RANK[a.status.key] - SOFTWARE_RANK[b.status.key]) || a.name.localeCompare(b.name, 'en', { numeric: true })),
  [devices, names, context, filters.spares])

  const matches = (row: Row) => {
    if (filters.show === 'all') return true
    const group = softwareShowFor(row.status.key)
    if (filters.show === 'notcurrent') return group !== 'current'
    return group === filters.show
  }
  const counts = useMemo(() => {
    const result: Record<SoftwareShow, number> = { attention: 0, updating: 0, newer: 0, current: 0, technician: 0 }
    for (const row of rows) {
      if (division !== 'all' && row.division !== division) continue
      const group = softwareShowFor(row.status.key)
      if (group) result[group] += 1
    }
    return result
  }, [rows, division])

  const findCampaign = (id?: string) => (id ? (live ?? []).find((row) => row.campaign_id === id) : undefined)
  const actionFor = (row: Row) => {
    const deployment = row.status.deployment
    const campaign = findCampaign(deployment?.update_id)
    const full = campaign?.deployments.find((item) => item.connector_id === row.device.connector_id && item.status === deployment?.status)
    if (row.status.key === 'failed' && campaign?.status === 'PAUSED' && full) {
      return (
        <span className="updates-row-actions">
          <Button size="sm" variant="primary" onClick={() => onRetry({ campaign, deployment: full })}>Try again</Button>
          <Button size="sm" onClick={() => onOpenUpdate(campaign)}>Open</Button>
        </span>
      )
    }
    return (
      <span className="updates-row-actions">
        <Button size="sm" aria-label={`Open ${row.name}`} onClick={() => (campaign ? onOpenUpdate(campaign) : onOpenDevice(row.device))}>Open</Button>
      </span>
    )
  }

  const panel = (key: DivisionKey) => {
    const inPanel = rows.filter((row) => row.division === key)
    const counted = inPanel.filter((row) => row.status.counted)
    const label = key === 'life' ? 'Life' : 'Health'
    const show = expanded[key] ? inPanel : inPanel.filter(matches)
    const upToDate = counted.filter((row) => row.status.key === 'current').length
    const summary = [
      `${upToDate} up to date`,
      ...(['updating', 'waiting', 'failed', 'technician', 'newer'] as const).map((group) => {
        const n = counted.filter((row) => row.status.key === group).length
        return n ? `${n} ${group === 'failed' ? 'update failed' : group === 'technician' ? (n === 1 ? 'needs a technician' : 'need a technician') : group === 'newer' ? 'can get a newer version' : group}` : ''
      }).filter(Boolean),
    ].join(' · ')
    return (
      <section key={key} className="updates-panel" aria-labelledby={`updates-panel-${key}`}>
        <header className="updates-panel-header">
          <h2 id={`updates-panel-${key}`}>
            <DivisionTag division={key} /> <span>{counted.length === 1 ? '1 device' : `${counted.length} devices`}</span>
          </h2>
          {loading ? <Skeleton width="50%" /> : <p>{summary}</p>}
        </header>
        {loading ? <Skeleton variant="row" count={3} /> : (
          <>
            {!show.length && filters.show === 'notcurrent' && counted.length > 0 && (
              <p className="updates-panel-empty"><Icon name="check" /> {counted.length === 1 ? `The ${label} device is up to date.` : `All ${counted.length} ${label} devices are up to date.`}</p>
            )}
            {!show.length && filters.show !== 'notcurrent' && <p className="updates-panel-empty">No {label} devices match.</p>}
            {!counted.length && !inPanel.length && <p className="updates-panel-empty">No {label} devices yet.</p>}
            <ul className="updates-device-list">
              {show.map((row) => (
                <li key={row.device.connector_id} className="updates-device-row" data-status={row.status.key}>
                  <div className="updates-device-name">
                    <strong>{row.name}</strong>
                    <span className="updates-device-version">
                      {row.status.deployment && row.status.version && row.status.deployment.target_version
                        ? `${row.status.version} → ${plainVersion(row.status.deployment.target_version)}`
                        : row.status.version ?? ''}
                    </span>
                  </div>
                  <div className="updates-device-state">
                    <StatusChip tone={row.status.chip} label={row.status.label} code={row.device.ota_state ?? undefined} size="sm" />
                    {row.status.sentence && <p>{row.status.sentence}</p>}
                  </div>
                  {actionFor(row)}
                </li>
              ))}
            </ul>
            {inPanel.length > 0 && (
              <Button variant="link" onClick={() => setExpanded((value) => ({ ...value, [key]: !value[key] }))}>
                {expanded[key] ? 'Show fewer devices' : `Show all ${inPanel.length} devices`}
              </Button>
            )}
          </>
        )}
      </section>
    )
  }

  const running = (live ?? []).filter((row) => row.status === 'ACTIVE' && (division === 'all' || updatePlace(row, names).division === division))
  const families = (['zkt', 'hikvision'] as const).map((family) => {
    const version = newestVersion(catalog?.rows, family)
    const release = catalog?.rows.find((row) => row.state === 'AVAILABLE' && row.version === version)
    return { family, version, on: release ? devicesOnVersion(release, devices).total : 0, release }
  })

  return (
    <div className="updates-overview">
      <FilterBar
        chipsLabel="Show devices"
        chips={(Object.keys(SOFTWARE_SHOW) as SoftwareShow[]).map((id) => ({ id, label: SOFTWARE_SHOW[id], count: loading ? null : counts[id] }))}
        chipValue={filters.show}
        onChipChange={(id) => setFilters({ show: filters.show === id ? 'notcurrent' : (id as SoftwareShow) })}
        active={filters.spares ? [{ id: 'spares', label: 'Spare devices shown', onRemove: () => setFilters({ spares: false }) }] : []}
        onClearAll={filters.show !== 'notcurrent' || filters.spares ? () => setFilters({ show: 'notcurrent', spares: false }) : undefined}
        actions={
          <label className="updates-check">
            <input type="checkbox" checked={filters.spares} onChange={(event) => setFilters({ spares: event.target.checked })} /> Show spare devices
          </label>
        }
      />
      <div className="updates-panels">
        {(division === 'all' || division === 'life') && panel('life')}
        {(division === 'all' || division === 'health') && panel('health')}
      </div>
      <section className="updates-running" aria-label="Updates running now">
        <h2>Updates running now</h2>
        {live === null ? <Skeleton width="40%" /> : running.length ? (
          <ul>
            {running.slice(0, 4).map((row) => {
              const place = updatePlace(row, names)
              const deployment = row.deployments.find((item) => !['SUCCEEDED', 'CANCELLED', 'SUPERSEDED'].includes(item.status))
              const step = deployment && { ...deployment, image_size: catalog?.rows.find((release) => release.release_id === row.release_id)?.image_size ?? null }
              return (
                <li key={row.campaign_id}>
                  <button type="button" className="updates-running-link" onClick={() => onOpenUpdate(row)}>
                    <strong>{place.name}</strong> ({place.division === 'life' ? 'Life' : 'Health'}) — {row.waiting ? 'Waiting for device' : step ? updateStepWord(step.status, downloadPercent(step)) : updateChip(row).label}
                  </button>
                </li>
              )
            })}
          </ul>
        ) : <p>{SOFTWARE_PAGE.noRunning}</p>}
        <Button variant="link" onClick={() => onGo('updates')}>See all updates</Button>
      </section>
      <section className="updates-newest" aria-label="Newest versions">
        <h2>Newest versions</h2>
        {catalogError && !catalog ? (
          <ErrorState compact title={SOFTWARE_PAGE.catalogFailed} onRetry={onRetryCatalog} />
        ) : catalog === null ? <Skeleton width="50%" /> : (
          <ul>
            {families.map(({ family, version, on }) => (
              <li key={family}>
                <span data-brand>{FAMILY_NAMES[family]}</span> link boxes: {version ? <><strong>{version}</strong> · on {on === 1 ? '1 device' : `${on} devices`}</> : SOFTWARE_PAGE.noNewest}
              </li>
            ))}
          </ul>
        )}
        <div className="updates-links">
          <Button variant="link" onClick={() => onGo('versions')}>See all versions</Button>
          <Button variant="link" icon="plus" onClick={onSetup}>{SOFTWARE_PAGE.setupLink}</Button>
        </div>
      </section>
      <TechnicalDetails>
        <ul className="updates-technical-list">
          <li>Updates for all devices: {catalog ? (catalog.enabled ? 'On' : 'Off') : 'not known yet'}</li>
          <li>Updates for test devices only: {catalog ? (catalog.hilEnabled ? 'On' : 'Off') : 'not known yet'}</li>
          {testPlan.map((line) => <li key={line}>Test plan {line}</li>)}
          {families.filter(({ release }) => release).map(({ family, release }) => (
            <li key={family}>{FAMILY_NAMES[family]} {release!.version}: signing key {release!.signing_key_id} · storage layout {release!.partition_layout}</li>
          ))}
        </ul>
      </TechnicalDetails>
    </div>
  )
}
