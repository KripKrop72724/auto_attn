// Recover several devices (08-advanced 1.8): a sheet with Life and Health
// groups, cities inside them, spares left out, indeterminate "Choose all",
// chunk progress and a result on every row. One click starts them all; the
// request body is unchanged.
import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, DivisionTag, Sheet } from '../../ui'
import { formatCount, plural } from '../../words/glossary'
import { BULK_OUTCOME, bulkReason, bulkSummary } from '../../words/recover'
import type { Device, ReconciliationBulkResult } from '../../types'
import type { Namer, Place } from './names'

type Row = { device: Device; place: Place }

function GroupCheck({ label, ids, selection, onToggle }: { label: string; ids: string[]; selection: Set<string>; onToggle: (ids: string[], checked: boolean) => void }) {
  const ref = useRef<HTMLInputElement>(null)
  const chosen = ids.filter((id) => selection.has(id)).length
  const all = ids.length > 0 && chosen === ids.length
  useEffect(() => { if (ref.current) ref.current.indeterminate = chosen > 0 && !all }, [all, chosen])
  return (
    <label className="recover-bulk-check is-group">
      <input ref={ref} type="checkbox" checked={all} disabled={!ids.length} onChange={(event) => onToggle(ids, event.target.checked)} />
      <span>{label}</span>
    </label>
  )
}

export function BulkRecoverSheet({ devices, namer, onStart, onClose }: {
  devices: Device[]
  namer: Namer
  onStart: (ids: string[], progress: (done: number, total: number) => void) => Promise<ReconciliationBulkResult[]>
  onClose: () => void
}) {
  const [selection, setSelection] = useState<Set<string>>(() => new Set())
  const [query, setQuery] = useState('')
  const [onlineOnly, setOnlineOnly] = useState(false)
  const [spares, setSpares] = useState(false)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [results, setResults] = useState<Map<string, ReconciliationBulkResult>>(() => new Map())
  const rows = useMemo<Row[]>(() => {
    const needle = query.trim().toLowerCase()
    return devices
      .map((device) => ({ device, place: namer(device) }))
      .filter(({ device, place }) => (spares || !device.is_spare)
        && (!onlineOnly || device.connected)
        && `${place.name} ${place.city} ${device.display_name} ${device.zone_id} ${device.zkt?.serial || ''}`.toLowerCase().includes(needle))
      .sort((left, right) => left.place.name.localeCompare(right.place.name, undefined, { numeric: true }))
  }, [devices, namer, onlineOnly, query, spares])
  const groups = (['life', 'health'] as const).map((division) => {
    const inGroup = rows.filter((row) => row.place.division === division)
    const cities = new Map<string, Row[]>()
    inGroup.forEach((row) => cities.set(row.place.city, [...(cities.get(row.place.city) || []), row]))
    return { division, rows: inGroup, cities: [...cities.entries()].sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true })) }
  })
  const toggle = (ids: string[], checked: boolean) => setSelection((current) => {
    const next = new Set(current)
    ids.forEach((id) => (checked ? next.add(id) : next.delete(id)))
    return next
  })
  const chosen = devices.filter((device) => selection.has(device.connector_id))
  const life = chosen.filter((device) => namer(device).division === 'life').length
  const health = chosen.length - life
  const counts = { CREATED: 0, ALREADY_ACTIVE: 0, BLOCKED: 0 }
  results.forEach((row) => { counts[row.outcome] += 1 })
  const start = async () => {
    const ids = [...selection]
    setBusy(true)
    setProgress({ done: 0, total: ids.length })
    try {
      const outcome = await onStart(ids, (done, total) => setProgress({ done, total }))
      setResults(new Map(outcome.map((row) => [row.connector_id, row])))
      setSelection(new Set())
    } finally {
      setBusy(false)
      setProgress(null)
    }
  }
  const label = chosen.length === 1 ? 'Start 1 recovery' : `Start ${formatCount(chosen.length)} recoveries`
  const split = chosen.length ? ` (${formatCount(life)} Life · ${formatCount(health)} Health)` : ''
  const footer = (
    <div className="recover-bulk-footer">
      {progress && (
        <div className="recover-bulk-progress" role="status">
          <span>{`Starting ${formatCount(progress.done)} of ${formatCount(progress.total)}…`}</span>
          <div className="recover-bar-track"><i style={{ width: `${progress.total ? Math.round((100 * progress.done) / progress.total) : 0}%` }} /></div>
        </div>
      )}
      {results.size > 0 && !progress && <p className="recover-bulk-summary" role="status">{bulkSummary(counts.CREATED, counts.ALREADY_ACTIVE, counts.BLOCKED)}</p>}
      <Button variant="primary" fullWidth busy={busy} busyLabel="Starting…" disabled={!selection.size || busy} onClick={() => start()}>
        {`${label}${split}`}
      </Button>
    </div>
  )
  return (
    <Sheet
      title="Recover several devices"
      description="Tick devices, or a whole city, and press Start. Each device is recovered on its own; if one can't start, the others still run."
      onClose={onClose}
      footer={footer}
      className="recover-sheet recover-bulk"
    >
      <div className="recover-bulk-tools" aria-disabled={busy || undefined}>
        <label className="search-field recover-search">
          <span className="sr-only">Search devices to recover</span>
          <input type="search" value={query} disabled={busy} onChange={(event) => setQuery(event.target.value)} placeholder="Search by place or serial number" />
        </label>
        <label className="recover-switch"><input type="checkbox" checked={onlineOnly} disabled={busy} onChange={(event) => setOnlineOnly(event.target.checked)} /><span>Online only</span></label>
        <label className="recover-switch"><input type="checkbox" checked={spares} disabled={busy} onChange={(event) => setSpares(event.target.checked)} /><span>Show spare devices</span></label>
      </div>
      <fieldset className="recover-bulk-groups" disabled={busy}>
        <legend className="sr-only">Devices</legend>
        {groups.map((group) => (
          <section key={group.division} className="recover-bulk-group" aria-label={`${group.division === 'life' ? 'Life' : 'Health'} (${group.rows.length})`}>
            <h3><DivisionTag division={group.division} /> <span>{`${group.division === 'life' ? 'Life' : 'Health'} (${formatCount(group.rows.length)})`}</span></h3>
            <GroupCheck label={`Choose all ${group.division === 'life' ? 'Life' : 'Health'} (${formatCount(group.rows.length)})`} ids={group.rows.filter((row) => !row.device.is_spare).map((row) => row.device.connector_id)} selection={selection} onToggle={toggle} />
            {group.cities.map(([city, cityRows]) => (
              <div key={city} className="recover-bulk-city">
                {cityRows.length > 1 && <GroupCheck label={`${city} (${plural(cityRows.length, 'device')})`} ids={cityRows.map((row) => row.device.connector_id)} selection={selection} onToggle={toggle} />}
                <ul>
                  {cityRows.map(({ device, place }) => {
                    const result = results.get(device.connector_id)
                    const words = result ? BULK_OUTCOME[result.outcome] : null
                    return (
                      <li key={device.connector_id} data-outcome={result?.outcome}>
                        <label className="recover-bulk-check">
                          <input type="checkbox" checked={selection.has(device.connector_id)} onChange={(event) => toggle([device.connector_id], event.target.checked)} />
                          <span>
                            <strong>{place.name}</strong>
                            <small>{device.is_spare ? 'Spare (not in use)' : device.connected ? 'Online' : 'Offline'}</small>
                          </span>
                        </label>
                        {words && (
                          <span className="recover-bulk-result">
                            <span aria-hidden="true">{words.icon} </span>
                            {result?.outcome === 'BLOCKED' ? `Couldn't start: ${bulkReason(result.message)}.` : words.label}
                          </span>
                        )}
                      </li>
                    )
                  })}
                </ul>
              </div>
            ))}
            {!group.rows.length && <p className="recover-muted">No devices match your search.</p>}
          </section>
        ))}
      </fieldset>
    </Sheet>
  )
}
