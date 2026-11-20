// Choose a device (07-people P1). Every device at once, grouped Life, Health
// (then spares when shown), sorted by place name so rows never move. Used as
// the /people page and inside the "Change device" sheet.
import { useMemo, type MouseEvent } from 'react'
import type { Device } from '../../types'
import { DataList, DivisionScopeChip, DivisionTag, EmptyState, ErrorState, FilterBar, Skeleton, StatusChip, toneForPattern, type DataColumn } from '../../ui'
import type { DivisionChoice } from '../../lib/division'
import { ago } from '../../lib/time'
import { deviceStatus } from '../../words/deviceStatus'
import { toneToPattern } from '../../words/types'
import { PICKER } from '../../words/people'
import { deviceNameMap, divisionOf, type PeopleSummaryRow } from './model'
import './people.css'

export type PickerShow = '' | 'fixing' | 'admin-on'

type PickerRow = {
  device: Device
  name: string
  division: 'life' | 'health'
  spare: boolean
  summary: PeopleSummaryRow | null
  offline: boolean
}

const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' })
const DIVISION_LABEL = { life: 'Life', health: 'Health' } as const

export interface DevicePickerListProps {
  devices: Device[]
  loaded: boolean
  error?: unknown
  onRetry?: () => unknown
  summary: Map<string, PeopleSummaryRow> | null
  summaryFailed: boolean
  query: string
  onQueryChange: (value: string) => void
  show: PickerShow
  onShowChange: (value: PickerShow) => void
  spares: boolean
  onSparesChange: (value: boolean) => void
  division: DivisionChoice
  onShowAllDivisions?: () => void
  selectedId?: string
  hrefFor: (connectorId: string) => string
  onPick: (connectorId: string, event: MouseEvent<HTMLElement>) => void
}

export function DevicePickerList({
  devices, loaded, error, onRetry, summary, summaryFailed, query, onQueryChange, show, onShowChange, spares, onSparesChange,
  division, onShowAllDivisions, selectedId, hrefFor, onPick,
}: DevicePickerListProps) {
  const names = useMemo(() => deviceNameMap(devices), [devices])
  const all = useMemo<PickerRow[]>(() => devices.map((device) => {
    const spare = Boolean(device.is_spare)
    const base = names.get(device.connector_id) || device.display_name
    return {
      device,
      name: spare && !/spare/i.test(base) && !device.friendly_name ? `${base} · Spare` : base,
      division: divisionOf(device),
      spare,
      summary: summary?.get(device.connector_id) ?? null,
      offline: deviceStatus(device).group === 'offline',
    }
  }), [devices, names, summary])

  const showCounts = Boolean(summary) && !summaryFailed
  const inScope = all.filter((row) => division === 'all' || row.division === division)
  const needle = query.trim().toLowerCase()
  const matches = (row: PickerRow) => !needle || [
    row.name, row.device.display_name, row.device.zone_name, row.device.zone_id, row.device.zkt?.serial || '', DIVISION_LABEL[row.division],
  ].join(' ').toLowerCase().includes(needle)
  const chipMatch = (row: PickerRow) => show === 'fixing' ? (row.summary?.needs_fixing ?? 0) > 0
    : show === 'admin-on' ? Boolean(row.summary?.admin_access) : true
  const filtered = inScope.filter((row) => matches(row) && (!showCounts || chipMatch(row)))
  const active = filtered.filter((row) => !row.spare)
  const spareRows = filtered.filter((row) => row.spare)
  const spareTotal = inScope.filter((row) => row.spare).length
  const order = (rows: PickerRow[]) => [...rows].sort((left, right) =>
    (left.division === right.division ? 0 : left.division === 'life' ? -1 : 1)
    || collator.compare(left.name, right.name)
    || (left.device.connector_id < right.device.connector_id ? -1 : 1))
  const shown = [...order(active), ...(spares ? order(spareRows) : [])]
  const groupCount = (key: 'life' | 'health' | 'spare') => key === 'spare' ? spareRows.length : active.filter((row) => row.division === key).length
  const groupOf = (row: PickerRow) => {
    const key = row.spare ? 'spare' : row.division
    const label = row.spare ? PICKER.spareGroup : DIVISION_LABEL[row.division]
    return { id: key, label: PICKER.group(label, groupCount(key)) }
  }

  const fixingCount = inScope.filter((row) => !row.spare && (row.summary?.needs_fixing ?? 0) > 0).length
  const adminCount = inScope.filter((row) => !row.spare && row.summary?.admin_access).length

  const columns: DataColumn<PickerRow>[] = [
    {
      id: 'device', header: PICKER.columns.device, primary: true,
      cell: (row) => (
        <span className="people-device-name">
          <strong data-user-text>{row.name}</strong>
          <DivisionTag division={row.division} />
          <small className="people-device-code" data-device-code>{row.device.display_name}</small>
        </span>
      ),
    },
    {
      id: 'status', header: PICKER.columns.status, card: 'chips',
      cell: (row) => {
        if (row.spare) return <span className="people-device-status"><StatusChip tone="neutral" label={PICKER.spareGroup} code="SPARE" size="sm" /></span>
        const status = deviceStatus(row.device)
        return (
          <span className="people-device-status">
            <StatusChip tone={toneForPattern(toneToPattern(status.tone))} label={status.chip} code={status.code} size="sm" />
            <span className="people-device-people-inline">{row.device.zkt ? PICKER.people(row.device.zkt.user_count ?? row.summary?.people) : PICKER.noMachine}</span>
          </span>
        )
      },
    },
    {
      id: 'people', header: PICKER.columns.people, card: 'hidden', align: 'end',
      cell: (row) => row.device.zkt ? (row.device.zkt.user_count ?? row.summary?.people ?? '–').toLocaleString('en-PK') : '—',
    },
    ...(showCounts ? [{
      id: 'fixing', header: PICKER.columns.fixing, card: 'hidden' as const, align: 'end' as const,
      cell: (row: PickerRow) => (row.summary?.needs_fixing ? <span className="people-fixing-count">{row.summary.needs_fixing.toLocaleString('en-PK')}</span> : '—'),
    }] : []),
    {
      id: 'read', header: PICKER.columns.listRead, card: 'hidden',
      cell: (row) => {
        const at = row.device.zkt?.identity_snapshot_observed_at || row.summary?.list_read_at
        return at ? ago(at) : '—'
      },
    },
    {
      id: 'note', header: PICKER.columns.open, card: 'reason', hideHeader: true,
      cell: (row) => {
        if (row.spare) return null
        if (showCounts && row.summary?.needs_fixing) return <span className="people-device-note tone-warning">{PICKER.needFixing(row.summary.needs_fixing)}</span>
        if (showCounts && row.summary?.admin_access) return <span className="people-device-note">{PICKER.adminAccessOn}</span>
        if (row.offline) return <span className="people-device-note">{PICKER.offlineLine}</span>
        return null
      },
    },
  ]

  const searchBar = (
    <FilterBar
      className="people-picker-filters"
      search={{ value: query, onChange: onQueryChange, label: PICKER.searchLabel, placeholder: PICKER.searchPlaceholder }}
      chips={showCounts ? [
        { id: 'fixing', label: PICKER.hasFixes, count: fixingCount },
        { id: 'admin-on', label: PICKER.adminOn, count: adminCount },
      ] : undefined}
      chipValue={show}
      chipsLabel={PICKER.chipsLabel}
      onChipChange={(id) => onShowChange(show === id ? '' : id as PickerShow)}
      result={loaded && shown.length ? { shown: shown.length, total: inScope.filter((row) => spares || !row.spare).length } : undefined}
    />
  )

  if (!loaded && !devices.length) {
    return (
      <div className="people-picker" aria-busy="true">
        {searchBar}
        <div className="people-device-skeleton">
          <h3 className="ui-datalist-group">{PICKER.group(DIVISION_LABEL.life, null)}</h3>
          <Skeleton variant="row" count={3} label={PICKER.loading} />
          <h3 className="ui-datalist-group">{PICKER.group(DIVISION_LABEL.health, null)}</h3>
          <Skeleton variant="row" count={3} />
        </div>
      </div>
    )
  }

  if (error && !devices.length) {
    return (
      <div className="people-picker">
        <ErrorState title={PICKER.loadFailed} message={PICKER.loadFailedHelp} error={error} onRetry={onRetry} />
      </div>
    )
  }

  let empty = null
  if (!shown.length) {
    if (needle) {
      empty = <EmptyState icon="search" title={PICKER.noMatch(query.trim())} action={<button type="button" className="button secondary" onClick={() => onQueryChange('')}>{PICKER.clearSearch}</button>}>{PICKER.noMatchHelp}</EmptyState>
    } else if (division !== 'all' && !inScope.length) {
      empty = <EmptyState icon="server" title={PICKER.noDivision(DIVISION_LABEL[division])} action={onShowAllDivisions && <button type="button" className="button secondary" onClick={onShowAllDivisions}>{PICKER.showAll}</button>}>{PICKER.noDivisionHelp}</EmptyState>
    } else if (show) {
      empty = <EmptyState icon="check" title={PICKER.noneForChip} action={<button type="button" className="button secondary" onClick={() => onShowChange('')}>{PICKER.showAll}</button>} />
    } else if (!spares && spareTotal) {
      empty = null
    } else {
      empty = <EmptyState icon="server" title={PICKER.noDevices} />
    }
  }

  return (
    <div className="people-picker">
      {searchBar}
      {onShowAllDivisions && <DivisionScopeChip division={division} onShowAll={onShowAllDivisions} />}
      {empty || (
        <DataList
          className="people-device-list"
          label={PICKER.listLabel}
          rows={shown}
          columns={columns}
          getKey={(row) => row.device.connector_id}
          groupBy={groupOf}
          getHref={(row) => hrefFor(row.device.connector_id)}
          onRowClick={(row, event) => onPick(row.device.connector_id, event)}
          rowLabel={(row) => `${PICKER.open(row.name)}${row.device.connector_id === selectedId ? ' (open now)' : ''}`}
        />
      )}
      {spareTotal > 0 && (
        <button type="button" className="text-button people-spares-toggle" aria-expanded={spares} onClick={() => onSparesChange(!spares)}>
          {spares ? PICKER.hideSpares : PICKER.showSpares(spareRows.length || spareTotal)}
        </button>
      )}
    </div>
  )
}
