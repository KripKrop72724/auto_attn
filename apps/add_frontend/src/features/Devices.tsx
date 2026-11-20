// Devices (`/devices`, 03-home-devices §2-3): every attendance device, the ones
// that need attention first. Every filter runs in the browser over the device
// store and lives in the page address, so a filtered view can be shared.
import { useMemo, type MouseEvent } from 'react'
import { useLocation } from 'react-router-dom'
import { useAppData } from '../AppData'
import { flag, oneOf, text, useUrlState } from '../lib/urlState'
import { routePath } from '../routing'
import type { Device } from '../types'
import { DataList, type DataColumn } from '../ui/DataList'
import { DivisionTag } from '../ui/DivisionTag'
import { EmptyState } from '../ui/EmptyState'
import { ErrorState } from '../ui/ErrorState'
import { FilterBar, type ActiveFilter } from '../ui/FilterBar'
import { PageHeader } from '../ui/PageHeader'
import { Skeleton } from '../ui/Skeleton'
import { useIsPhone } from '../ui/useMediaQuery'
import { DEVICES_WORDS, HOME_WORDS, REFRESH_WORDS } from '../words/home'
import { DeviceStatusChip, UpdatedAgo, lastSeenText, sinceText, useNow } from './devices/DeviceBits'
import {
  BRAND_WORD, applyFilters, cityOptions, deviceViews, sortViews, spareCount, statusCounts, versionOptions,
  type Brand, type DeviceFilters, type DeviceView, type SortKey, type StatusFilter,
} from './devices/deviceList'
import { useDivision } from '../lib/useDivision'
import { useStatusChange } from './devices/useStatusChange'
import './Devices.css'

const STATUS_VALUES = ['all', 'attention', 'offline', 'working', 'setup'] as const
const SORT_VALUES = ['attention', 'name', 'seen'] as const
const BRAND_VALUES = ['all', 'zkteco', 'hikvision'] as const

const schema = {
  status: oneOf(STATUS_VALUES, 'all'),
  // Several cities, "Karachi,Lahore" (one key; a comma never appears in a city name).
  city: text('', { typing: false, maxLength: 400 }),
  brand: oneOf(BRAND_VALUES, 'all'),
  spare: flag(),
  note: flag(),
  version: text('', { typing: false, maxLength: 40 }),
  q: text(''),
  sort: oneOf(SORT_VALUES, 'attention'),
}

const splitCities = (value: string) => value.split(',').map((city) => city.trim()).filter(Boolean)

export function DevicesView() {
  const app = useAppData()
  const location = useLocation()
  const phone = useIsPhone()
  const now = useNow()
  const [division] = useDivision()
  const [values, setValues] = useUrlState(schema)
  const views = useMemo(() => deviceViews(app.devices), [app.devices])
  const filters: DeviceFilters = useMemo(() => ({
    division,
    status: values.status as StatusFilter,
    cities: splitCities(values.city),
    brand: values.brand as Brand | 'all',
    spare: values.spare,
    version: values.version,
    note: values.note,
    q: values.q,
  }), [division, values])
  const sort = values.sort as SortKey
  const counts = useMemo(() => statusCounts(views, filters), [filters, views])
  const spares = useMemo(() => spareCount(views, filters), [filters, views])
  const cities = useMemo(() => cityOptions(views, filters), [filters, views])
  const versions = useMemo(() => versionOptions(views, filters), [filters, views])
  const shown = useMemo(() => sortViews(applyFilters(views, filters), filters.spare ? 'name' : sort), [filters, sort, views])
  const { changed, announcement } = useStatusChange(views, app.devicesLoaded)
  const totalInScope = filters.spare ? spares : counts.all

  const setCities = (next: string[]) => setValues({ city: next.join(',') })
  const toggleCity = (city: string) => setCities(filters.cities.includes(city) ? filters.cities.filter((item) => item !== city) : [...filters.cities, city])
  const clearAll = () => setValues({ status: 'all', city: '', brand: 'all', spare: false, note: false, version: '', q: '' })

  const active: ActiveFilter[] = [
    ...filters.cities.map((city) => ({ id: `city-${city}`, label: DEVICES_WORDS.activeCity(city), onRemove: () => setCities(filters.cities.filter((item) => item !== city)) })),
    ...(filters.brand !== 'all' ? [{ id: 'brand', label: DEVICES_WORDS.activeBrand(BRAND_WORD[filters.brand]), onRemove: () => setValues({ brand: 'all' }) }] : []),
    ...(filters.version ? [{ id: 'version', label: DEVICES_WORDS.versionActive(filters.version), onRemove: () => setValues({ version: '' }) }] : []),
    ...(filters.note ? [{ id: 'note', label: DEVICES_WORDS.noteChip, onRemove: () => setValues({ note: false }) }] : []),
  ]
  const filtered = active.length > 0 || filters.status !== 'all' || filters.spare || Boolean(filters.q.trim())
  const moreCount = filters.cities.length + (filters.brand !== 'all' ? 1 : 0) + (filters.version ? 1 : 0)

  const chips = [
    ...(filters.spare ? [] : STATUS_VALUES
      .filter((id) => id !== 'setup' || counts.setup > 0 || filters.status === 'setup')
      .map((id) => ({ id, label: DEVICES_WORDS.statusChips[id], count: app.devicesLoaded ? counts[id] : null }))),
    { id: 'spare', label: DEVICES_WORDS.spareChip, count: app.devicesLoaded ? spares : null },
  ]
  const onChip = (id: string) => {
    if (id === 'spare') setValues({ spare: !filters.spare })
    else setValues({ status: id as StatusFilter })
  }

  const openRow = (row: DeviceView, event: MouseEvent<HTMLElement>) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return
    event.preventDefault()
    app.inspectDevice(row.device, shown.map((item) => item.id))
  }
  const openPeople = (device: Device, event: MouseEvent<HTMLAnchorElement>) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return
    event.preventDefault()
    app.manageUsers(device)
  }

  const columns: DataColumn<DeviceView>[] = [
    {
      id: 'device',
      header: DEVICES_WORDS.columns.device,
      primary: true,
      cell: (row) => (
        <span className="device-name-cell">
          <span className="device-name-line"><span className="device-name">{row.name}</span> <DivisionTag division={row.division} /></span>
          <small className="device-city">{row.city}</small>
          {row.brand && <small className="device-brand-inline" data-brand>{BRAND_WORD[row.brand]}</small>}
        </span>
      ),
    },
    {
      id: 'status',
      header: DEVICES_WORDS.columns.status,
      card: 'chips',
      className: 'device-status-cell',
      cell: (row) => (
        <span className="device-status-line">
          <DeviceStatusChip view={row} />
          {phone && row.status.since && row.status.group !== 'working' && <small className="device-since">{sinceText(row.status.since, now).replace(/^Since/, 'since')}</small>}
        </span>
      ),
    },
    {
      id: 'happening',
      header: DEVICES_WORDS.columns.happening,
      card: 'reason',
      className: 'device-happening-cell',
      cell: (row) => <ReasonText view={row} now={now} />,
    },
    {
      id: 'seen',
      header: DEVICES_WORDS.columns.lastSeen,
      card: 'hidden',
      className: 'device-seen-cell',
      cell: (row) => <span className="device-seen">{lastSeenText(row.device.last_seen_at, now)}</span>,
    },
    {
      id: 'machine',
      header: DEVICES_WORDS.columns.machine,
      card: 'hidden',
      className: 'device-machine-cell',
      cell: (row) => (
        <span className="device-machine">
          <small className="device-brand">{row.brand ? <span data-brand>{BRAND_WORD[row.brand]}</span> : DEVICES_WORDS.noMachine}</small>
          <a
            className="device-people-link"
            href={routePath('people', row.id, { division })}
            aria-label={DEVICES_WORDS.peopleOn(row.name)}
            onClick={(event) => openPeople(row.device, event)}
          >
            {DEVICES_WORDS.people} <span aria-hidden="true">›</span>
          </a>
        </span>
      ),
    },
  ]

  const firstLoadFailed = !app.devicesLoaded && Boolean(app.devicesError)
  let body
  if (firstLoadFailed) {
    body = <ErrorState title={REFRESH_WORDS.loadFailedTitle} message={REFRESH_WORDS.loadFailedBody} onRetry={app.refreshFleet} headingLevel={2} />
  } else if (!app.devicesLoaded) {
    body = <div className="devices-loading"><Skeleton variant="row" count={phone ? 4 : 6} label={HOME_WORDS.loadingDevices} /></div>
  } else if (!app.devices.length) {
    body = <EmptyState title={HOME_WORDS.emptyTitle} headingLevel={2}>{HOME_WORDS.emptyBody}</EmptyState>
  } else {
    body = (
      <DataList
        className="devices-list"
        rows={shown}
        columns={columns}
        getKey={(row) => row.id}
        label={DEVICES_WORDS.listLabel}
        getHref={(row) => `${routePath('devices', row.id)}${location.search}`}
        onRowClick={openRow}
        rowLabel={(row) => DEVICES_WORDS.rowLabel(row.name, row.division === 'life' ? 'Life' : 'Health', row.status.chip)}
        changedKeys={changed}
        empty={filters.spare && !filters.q && !active.length
          ? <EmptyState title={DEVICES_WORDS.noSpares} headingLevel={2}>{DEVICES_WORDS.noSparesBody}</EmptyState>
          : <EmptyState title={DEVICES_WORDS.noMatch} headingLevel={2} action={<button type="button" className="button secondary" onClick={clearAll}>{DEVICES_WORDS.clearAllFilters}</button>} />}
      />
    )
  }

  return (
    <div className="devices-page">
      <PageHeader title={DEVICES_WORDS.title} description={DEVICES_WORDS.description} primaryAction={<UpdatedAgo at={app.devicesUpdatedAt} />} />
      <FilterBar
        className="devices-filters"
        search={{ value: values.q, onChange: (q) => setValues({ q }), label: DEVICES_WORDS.searchLabel, placeholder: DEVICES_WORDS.searchPlaceholder }}
        chips={chips}
        chipValue={filters.spare ? ['spare'] : [filters.status]}
        onChipChange={onChip}
        chipsLabel={DEVICES_WORDS.status}
        sort={filters.spare ? undefined : {
          value: sort,
          label: DEVICES_WORDS.sort,
          options: SORT_VALUES.map((id) => ({ id, label: DEVICES_WORDS.sortOptions[id] })),
          onChange: (next) => setValues({ sort: next as SortKey }),
        }}
        moreFilters={(
          <div className="devices-more-filters">
            <fieldset>
              <legend>{DEVICES_WORDS.city}</legend>
              {cities.map((option) => (
                <label key={option.city} className="devices-check">
                  <input type="checkbox" checked={filters.cities.includes(option.city)} onChange={() => toggleCity(option.city)} />
                  <span>{option.city}</span>
                  <small>{option.count}</small>
                </label>
              ))}
            </fieldset>
            <fieldset>
              <legend>{DEVICES_WORDS.brand}</legend>
              {BRAND_VALUES.map((id) => (
                <label key={id} className="devices-check">
                  <input type="radio" name="devices-brand" checked={filters.brand === id} onChange={() => setValues({ brand: id })} />
                  <span>{id === 'all' ? DEVICES_WORDS.brandAny : <span data-brand>{BRAND_WORD[id]}</span>}</span>
                </label>
              ))}
            </fieldset>
            <label className="devices-check devices-switch">
              <input type="checkbox" role="switch" checked={filters.spare} onChange={(event) => setValues({ spare: event.target.checked })} />
              <span>{DEVICES_WORDS.spareSwitch}</span>
            </label>
            <fieldset>
              <legend>{DEVICES_WORDS.version}</legend>
              <label className="devices-check">
                <input type="radio" name="devices-version" checked={!filters.version} onChange={() => setValues({ version: '' })} />
                <span>{DEVICES_WORDS.versionAny}</span>
              </label>
              {versions.map((option) => (
                <label key={option.id} className="devices-check">
                  <input type="radio" name="devices-version" checked={filters.version === option.id} onChange={() => setValues({ version: option.id })} />
                  <span>{option.version ? DEVICES_WORDS.versionOption(option.version, option.count) : DEVICES_WORDS.versionUnknown}{option.version && option.hikvision && <> <span data-brand>{DEVICES_WORDS.versionBrand}</span></>}</span>
                </label>
              ))}
            </fieldset>
          </div>
        )}
        moreFiltersCount={moreCount}
        moreLabel={phone ? DEVICES_WORDS.filtersCount(moreCount) : undefined}
        showResultsLabel={DEVICES_WORDS.showDevices(shown.length)}
        active={active}
        onClearAll={filtered ? clearAll : undefined}
        result={app.devicesLoaded ? { shown: shown.length, total: totalInScope } : undefined}
      />
      {app.devicesLoaded && !filtered && <p className="devices-total">{DEVICES_WORDS.unfiltered(counts.all)}</p>}
      <p className="sr-only" aria-live="polite">{announcement}</p>
      <section className="panel devices-panel" aria-label={DEVICES_WORDS.listLabel} aria-busy={!app.devicesLoaded || undefined}>
        {body}
      </section>
    </div>
  )
}

function ReasonText({ view, now }: { view: DeviceView; now: number }) {
  const status = view.status
  if (status.group === 'spare') return <span className="device-reason is-quiet">{DEVICES_WORDS.notCounted}</span>
  if (status.note) return <span className="device-reason is-quiet">{status.note}</span>
  if (!status.heading) return null
  const since = status.group !== 'working' && status.since ? sinceText(status.since, now) : ''
  return (
    <span className="device-reason">
      <strong>{status.heading}.</strong>{' '}
      <span>{status.sentence}</span>
      {since && <small className="device-reason-since">{since}</small>}
    </span>
  )
}
