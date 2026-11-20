// Update software (08-advanced 2.6, today dialog "Update firmware"): choose a
// version, then devices grouped Life / Health. One click starts the update in
// each place; requests go in groups of 30 with the same body as before.
import { useMemo, useRef, useState, type RefObject } from 'react'
import { api } from '../../api'
import { idempotency } from '../../lib/idempotency'
import type { Device, FirmwareBulkResult, FirmwareBulkZoneResult, FirmwareRelease } from '../../types'
import { Button, Callout, DivisionTag, Sheet, StatusChip } from '../../ui'
import type { Toast } from '../../ui/Toast'
import { BULK_RESULT, FAMILY_NAMES, SOFTWARE_PAGE, compareVersions, plainVersion, stabilityWarningLine, updateActionError, updateToast } from '../../words/software'
import { hilAllowedTargets, hilDevice } from '../hilTargets'
import { LIVE_STATES, isCurrentRelease, publishable, releaseFamily, type NameIndex, type UpdateRow } from './model'
import type { Catalog } from './UpdatesPage'

// The server accepts at most this many places per one-click request; more are sent in turn.
const BULK_ZONE_LIMIT = 30

type ZoneRow = {
  id: string
  name: string
  division: 'life' | 'health'
  devices: Device[]
  version: string | null
  online: boolean
  running: boolean
  wrongBrand: boolean
  already: boolean
}

function zonesFor(release: FirmwareRelease | null, devices: Device[], names: NameIndex, live: UpdateRow[]): ZoneRow[] {
  if (!release) return []
  const family = releaseFamily(release)
  const running = new Set(live.filter((row) => LIVE_STATES.includes(row.status)).map((row) => row.zone_id))
  let allowed: Set<string> | null = null
  if (release.state === 'HIL_ONLY') {
    const ids = release.hil_targets
      ? hilAllowedTargets(release).map((target) => devices.find((device) => device.connector_id === target.connector_id)?.zone_id)
      : [hilDevice(release, devices)?.zone_id]
    allowed = new Set(ids.filter(Boolean) as string[])
  }
  const grouped = new Map<string, Device[]>()
  for (const device of devices) {
    if (device.is_spare || !device.zone_id) continue
    if (allowed && !allowed.has(device.zone_id)) continue
    grouped.set(device.zone_id, [...(grouped.get(device.zone_id) ?? []), device])
  }
  return [...grouped.entries()].map(([id, members]) => {
    const named = names.zone(id)
    const versions = members.map((device) => device.firmware_version)
    return {
      id,
      name: named.name,
      division: named.division,
      devices: members,
      version: plainVersion(versions[0]),
      online: members.some((device) => device.connected),
      running: running.has(id),
      wrongBrand: members.every((device) => device.firmware_family && device.firmware_family !== family),
      already: members.every((device) => compareVersions(device.firmware_version, release.version) === 0),
    }
  }).sort((a, b) => a.name.localeCompare(b.name, 'en', { numeric: true }))
}

export default function UpdateSoftwareSheet({
  catalog, devices, names, live, toast, returnFocusRef, onClose, onStarted, onOpenUpdate, onDone,
}: {
  catalog: Catalog
  devices: Device[]
  names: NameIndex
  live: UpdateRow[]
  toast: Toast
  returnFocusRef?: RefObject<HTMLElement | null>
  onClose: () => void
  onStarted: () => Promise<void>
  onOpenUpdate: (campaignId: string) => void
  onDone: (campaignIds: string[]) => void
}) {
  const options = useMemo(
    () => catalog.rows.filter((release) => publishable(release, catalog.enabled, catalog.hilEnabled) && isCurrentRelease(release)),
    [catalog],
  )
  const [releaseId, setReleaseId] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [query, setQuery] = useState('')
  const [showAlready, setShowAlready] = useState(false)
  const [progress, setProgress] = useState<{ from: number; to: number; total: number } | null>(null)
  const [error, setError] = useState('')
  const [results, setResults] = useState<FirmwareBulkZoneResult[] | null>(null)
  const resultsRef = useRef<HTMLHeadingElement>(null)
  const release = options.find((row) => row.release_id === releaseId) || options[0] || null
  const zones = useMemo(() => zonesFor(release, devices, names, live), [release, devices, names, live])
  const busy = progress !== null
  const choosable = (zone: ZoneRow) => !zone.running && !zone.wrongBrand && !zone.already
  const term = query.trim().toLowerCase()
  const visible = zones.filter((zone) => (showAlready || !zone.already) && (!term || zone.name.toLowerCase().includes(term)))
  const chosen = zones.filter((zone) => selected.includes(zone.id))
  const lifeChosen = chosen.filter((zone) => zone.division === 'life').length

  const chooseRelease = (value: string) => {
    setReleaseId(value)
    setSelected([])
    setResults(null)
    setError('')
  }
  const toggle = (id: string) => setSelected((current) => (current.includes(id) ? current.filter((value) => value !== id) : [...current, id]))

  const start = async () => {
    if (!release || !selected.length || busy) return
    setError('')
    setResults(null)
    const collected: FirmwareBulkZoneResult[] = []
    try {
      for (let index = 0; index < selected.length; index += BULK_ZONE_LIMIT) {
        const part = selected.slice(index, index + BULK_ZONE_LIMIT)
        setProgress({ from: index + 1, to: index + part.length, total: selected.length })
        const response = await api<FirmwareBulkResult>('/api/v1/firmware/campaigns/bulk', {
          method: 'POST',
          body: JSON.stringify({ release_id: release.release_id, zone_ids: part, idempotency_key: idempotency('firmware-bulk') }),
        })
        collected.push(...response.results)
        setResults([...collected])
      }
      const created = collected.filter((row) => row.outcome === 'CREATED').length
      toast.notice(updateToast.started(release.version, created, collected.length))
      window.setTimeout(() => resultsRef.current?.focus(), 0)
    } catch (reason) {
      setError(updateActionError(reason instanceof Error ? reason.message : '', "The update couldn't start. Try again."))
    } finally {
      setProgress(null)
      if (collected.length) await onStarted()
    }
  }

  const zoneName = (id: string) => zones.find((zone) => zone.id === id)?.name ?? names.zone(id).name
  const resultText = (row: FirmwareBulkZoneResult) => {
    if (row.outcome === 'CREATED' && row.fleet_hold === 'NATIONWIDE_TARGET_OFFLINE') return `Started. It begins when ${zoneName(row.zone_id)} comes online.`
    if (row.outcome === 'CREATED' && row.fleet_hold) return 'Started. It begins when its turn comes.'
    if (row.outcome === 'CREATED') return row.eligible === 1 ? 'Started.' : `Started for ${row.eligible} devices.`
    if (row.outcome === 'BUSY') return 'This device already has an update running.'
    return updateActionError(row.message, "This device can't take this version.")
  }

  const group = (key: 'life' | 'health') => {
    const rows = visible.filter((zone) => zone.division === key)
    const choices = zones.filter((zone) => zone.division === key && choosable(zone))
    const picked = choices.filter((zone) => selected.includes(zone.id)).length
    const label = key === 'life' ? 'Life' : 'Health'
    if (!zones.some((zone) => zone.division === key)) return null
    return (
      <fieldset key={key} className="updates-pick-group" disabled={busy}>
        <legend><DivisionTag division={key} /> <span>({choices.length})</span></legend>
        {choices.length > 0 && (
          <label className="updates-check updates-choose-all">
            <input
              type="checkbox"
              checked={picked === choices.length}
              ref={(node) => { if (node) node.indeterminate = picked > 0 && picked < choices.length }}
              onChange={() => setSelected((current) => picked === choices.length
                ? current.filter((id) => !choices.some((zone) => zone.id === id))
                : [...new Set([...current, ...choices.map((zone) => zone.id)])])}
            />
            Choose all {label} ({choices.length})
          </label>
        )}
        <ul className="updates-pick-list">
          {rows.map((zone) => {
            const note = zone.already ? `Already on ${plainVersion(release?.version)}`
              : zone.running ? 'Update already running'
                : zone.wrongBrand ? 'This version is for the other machine brand'
                  : !zone.online ? 'Offline: it will update when back online'
                    : zone.version ? `Now on ${zone.version}` : 'Version not known yet'
            return (
              <li key={zone.id}>
                <label className="updates-check updates-pick-row" data-disabled={!choosable(zone) || undefined}>
                  <input type="checkbox" checked={selected.includes(zone.id)} disabled={!choosable(zone)} onChange={() => toggle(zone.id)} />
                  <span>
                    <strong>{zone.name}</strong>
                    <small>{note}</small>
                  </span>
                </label>
              </li>
            )
          })}
        </ul>
      </fieldset>
    )
  }

  const footer = (
    <div className="updates-sheet-actions">
      {results ? (
        <Button variant="primary" onClick={() => onDone(results.map((row) => row.campaign_id).filter(Boolean) as string[])}>Done</Button>
      ) : (
        <Button variant="primary" icon="update" busy={busy} busyLabel={progress ? `Starting ${progress.from}–${progress.to} of ${progress.total}…` : 'Starting…'} disabled={!release || !selected.length} onClick={() => start()}>
          {`Update ${selected.length === 1 ? '1 device' : `${selected.length} devices`}${selected.length ? ` (${lifeChosen} Life · ${selected.length - lifeChosen} Health)` : ''}`}
        </Button>
      )}
    </div>
  )

  return (
    <Sheet
      title={SOFTWARE_PAGE.updateSoftware}
      description="Choose a version and the devices. The update starts straight away."
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      className="updates-software-sheet"
      dismissOnBackdrop={!busy}
      footer={footer}
    >
      {!options.length ? (
        <Callout tone="warning" role="alert">{SOFTWARE_PAGE.noVersionReady}</Callout>
      ) : (
        <>
          <fieldset className="updates-version-choice" disabled={busy}>
            <legend>Choose a version</legend>
            {options.map((row) => {
              const family = releaseFamily(row)
              return (
                <label key={row.release_id} className="updates-radio-card" data-checked={row.release_id === release?.release_id || undefined}>
                  <input type="radio" name="update-version" checked={row.release_id === release?.release_id} onChange={() => chooseRelease(row.release_id)} />
                  <span>
                    <strong>{row.version}</strong> · <span data-brand>{FAMILY_NAMES[family]}</span> · {row.state === 'HIL_ONLY' ? 'Test devices only' : 'For all devices'}
                  </span>
                </label>
              )
            })}
          </fieldset>
          <section className="updates-device-choice" aria-label="Choose devices">
            <h3>Choose devices <small>{selected.length} of {zones.filter(choosable).length} chosen</small></h3>
            <label className="updates-search">
              <span className="sr-only">Search devices</span>
              <input type="search" value={query} placeholder="Search devices" onChange={(event) => setQuery(event.target.value)} disabled={busy} />
            </label>
            <label className="updates-check">
              <input type="checkbox" checked={showAlready} onChange={(event) => setShowAlready(event.target.checked)} disabled={busy} /> Show devices already on this version
            </label>
            {group('life')}
            {group('health')}
            {!zones.length && <p className="updates-helper">No device can take this version.</p>}
            {selected.length > 0 && <Button variant="link" onClick={() => setSelected([])} disabled={busy}>Clear</Button>}
          </section>
        </>
      )}
      {error && <Callout tone="danger" role="alert">{error}</Callout>}
      {results && (
        <section className="updates-results" aria-labelledby="updates-results-title">
          <h3 id="updates-results-title" tabIndex={-1} ref={resultsRef}>Results</h3>
          <ul aria-label="Update results">
            {results.map((row) => (
              <li key={row.zone_id}>
                <StatusChip
                  tone={row.outcome === 'CREATED' ? 'working' : row.outcome === 'BUSY' ? 'attention' : 'offline'}
                  label={row.outcome === 'CREATED' ? BULK_RESULT.CREATED : row.outcome === 'BUSY' ? BULK_RESULT.BUSY : BULK_RESULT.BLOCKED}
                  size="sm"
                  code={row.outcome}
                />
                <span>
                  <strong>{zoneName(row.zone_id)}</strong>
                  <small>{resultText(row)}</small>
                  {row.stability_warnings.map((warning) => (
                    <small key={`${warning.connector_id}-${warning.code}`} className="updates-warning">
                      {stabilityWarningLine(warning, names.device(warning.connector_id)?.name ?? zoneName(row.zone_id))}
                    </small>
                  ))}
                </span>
                {row.campaign_id && <Button size="sm" onClick={() => onOpenUpdate(row.campaign_id!)}>Open</Button>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </Sheet>
  )
}
