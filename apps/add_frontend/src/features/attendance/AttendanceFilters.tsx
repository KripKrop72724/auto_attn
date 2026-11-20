// The Attendance filter bar (ATT-7): search (name, Employee ID or CNIC),
// dates, place, status and "More filters". Desktop shows Dates, Place and
// Status as their own buttons; phones keep Dates and put Place and Status in
// the More filters sheet. Every value lives in the URL except a CNIC.
import { useMemo, useRef, useState, type ReactNode } from 'react'
import { AnchoredLayer } from '../../AnchoredLayer'
import { Icon } from '../../Icon'
import { FilterBar, useIsPhone, type ActiveFilter } from '../../ui'
import { deviceDivision } from '../../lib/division'
import { deviceName } from '../../lib/deviceNames'
import type { DivisionChoice } from '../../lib/prefs'
import type { Device } from '../../types'
import type { DeliveryGroup } from '../../words/attendance'
import { GROUP_CHIP, GROUP_ORDER, PHONE_FILTERS, PHONE_STATUS_WORDS, type PhoneFilter } from './attendanceWords'
import { cnicDigits, CLEARED, MAX_PLACES, moreFiltersCount, type FilterValues } from './useAttendanceFilters'
import { pktPresetRange, todayKey, type WhenPreset } from './pktDates'
import type { GroupTotals } from './types'

export type Place = { id: string; name: string; division: 'life' | 'health'; spare: boolean }

/** Places for the picker: friendly names, Life first then Health, "(spare)" after a spare. */
export function placesFrom(devices: Device[]): Place[] {
  return devices.map((device) => {
    const parsed = deviceName(device.display_name || device.zone_id)
    const name = device.friendly_name || (parsed.unit ? `${parsed.location} · ${parsed.unit}` : parsed.location)
    return { id: device.connector_id, name: device.is_spare ? `${name} (spare)` : name, division: deviceDivision(device), spare: Boolean(device.is_spare) }
  }).sort((a, b) => (a.division === b.division ? a.name.localeCompare(b.name, 'en', { numeric: true }) : a.division === 'life' ? -1 : 1))
}

const PRESETS: Array<{ id: WhenPreset; label: string }> = [
  { id: 'today', label: 'Today' }, { id: 'yesterday', label: 'Yesterday' }, { id: '7d', label: 'Last 7 days' }, { id: 'custom', label: 'Pick dates' },
]
const presetLabel = (when: WhenPreset) => PRESETS.find((item) => item.id === when)?.label || 'Today'

function Popover({ buttonLabel, buttonText, count, title, children }: { buttonLabel?: string; buttonText: string; count?: number; title: string; children: (close: () => void) => ReactNode }) {
  const ref = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const close = () => setOpen(false)
  return <>
    <button ref={ref} type="button" className="button secondary attendance-filter-button" aria-haspopup="dialog" aria-expanded={open} aria-label={buttonLabel} onClick={() => setOpen((value) => !value)}>
      <span>{buttonText}</span>{count ? <span className="ui-filter-more-count">{count}</span> : null}<Icon name="chevronDown" size="sm" />
    </button>
    {open && (
      <AnchoredLayer anchorRef={ref} mobileSheet preferredWidth={340} onDismiss={(reason) => { close(); if (reason === 'escape') ref.current?.focus() }}>
        <div className="ui-filter-popover attendance-popover" role="dialog" aria-label={title}>
          <header className="attendance-popover-head"><h3>{title}</h3><button type="button" className="icon-button" aria-label="Close" onClick={() => { close(); ref.current?.focus() }}><Icon name="x" /></button></header>
          {children(close)}
        </div>
      </AnchoredLayer>
    )}
  </>
}

function DatePicker({ filters, onChange }: { filters: FilterValues; onChange: (change: Partial<FilterValues>) => void }) {
  const [from, setFrom] = useState(filters.from || todayKey())
  const [to, setTo] = useState(filters.to || todayKey())
  const invalid = from && to && from > to
  const long = !invalid && from && to && pktPresetRange('custom', Date.now(), from, to).days > 31
  return <Popover buttonText={filters.when === 'custom' ? pktPresetRange('custom', Date.now(), filters.from, filters.to).label : presetLabel(filters.when)} buttonLabel={`Dates: ${presetLabel(filters.when)}`} title="Dates">
    {(close) => <div className="attendance-date-sheet">
      <div role="radiogroup" aria-label="Dates" className="attendance-date-presets">
        {PRESETS.map((preset) => (
          <label key={preset.id} className="attendance-radio">
            <input type="radio" name="attendance-when" checked={filters.when === preset.id} onChange={() => {
              if (preset.id === 'custom') onChange({ when: 'custom', from, to })
              else { onChange({ when: preset.id, from: '', to: '' }); close() }
            }} />{preset.label}
          </label>
        ))}
      </div>
      {filters.when === 'custom' && <div className="attendance-date-fields">
        <label>From<input type="date" value={from} max={todayKey()} onChange={(event) => { setFrom(event.target.value); if (event.target.value && (!to || event.target.value <= to)) onChange({ when: 'custom', from: event.target.value, to }) }} /></label>
        <label>To<input type="date" value={to} max={todayKey()} onChange={(event) => { setTo(event.target.value); if (event.target.value && (!from || from <= event.target.value)) onChange({ when: 'custom', from, to: event.target.value }) }} /></label>
        {invalid && <p className="attendance-field-error" role="alert">The start date must be on or before the end date.</p>}
        {long && <p className="attendance-field-note">Totals are shown for up to 31 days. Pick a shorter range to see them.</p>}
      </div>}
      <p className="attendance-field-note">Dates and times are in Pakistan time.</p>
    </div>}
  </Popover>
}

function PlaceList({ places, chosen, onChange }: { places: Place[]; chosen: string[]; onChange: (ids: string[]) => void }) {
  const [query, setQuery] = useState('')
  const [spares, setSpares] = useState(() => chosen.some((id) => places.find((place) => place.id === id)?.spare))
  const shown = places.filter((place) => (spares || !place.spare || chosen.includes(place.id)) && place.name.toLowerCase().includes(query.trim().toLowerCase()))
  const toggle = (id: string) => onChange(chosen.includes(id) ? chosen.filter((item) => item !== id) : [...chosen, id].slice(0, MAX_PLACES))
  return <div className="attendance-place-list">
    <label className="attendance-place-search"><span className="sr-only">Find a place</span><input type="search" value={query} placeholder="Find a place" onChange={(event) => setQuery(event.target.value)} /></label>
    {(['life', 'health'] as const).map((division) => {
      const items = shown.filter((place) => place.division === division)
      if (!items.length) return null
      return <fieldset key={division} className="attendance-place-group"><legend>{division === 'life' ? 'Life' : 'Health'}</legend>
        {items.map((place) => <label key={place.id} className="attendance-check"><input type="checkbox" checked={chosen.includes(place.id)} onChange={() => toggle(place.id)} />{place.name}</label>)}
      </fieldset>
    })}
    {!shown.length && <p className="attendance-field-note">No place matches.</p>}
    <label className="attendance-switch"><input type="checkbox" role="switch" checked={spares} onChange={(event) => setSpares(event.target.checked)} /> Include spare devices</label>
  </div>
}

function StatusList({ chosen, counts, onChange }: { chosen: DeliveryGroup[]; counts: GroupTotals | null; onChange: (groups: DeliveryGroup[]) => void }) {
  return <fieldset className="attendance-status-list"><legend>Status</legend>
    {GROUP_ORDER.map((group) => <label key={group} className="attendance-check"><input type="checkbox" checked={chosen.includes(group)} onChange={(event) => onChange(event.target.checked ? [...chosen, group] : chosen.filter((item) => item !== group))} />{GROUP_CHIP[group].label}{counts ? <span className="attendance-check-count">{counts[group].toLocaleString('en-PK')}</span> : null}</label>)}
  </fieldset>
}

function MoreFields({ filters, onChange }: { filters: FilterValues; onChange: (change: Partial<FilterValues>) => void }) {
  const phone = (value: PhoneFilter) => onChange({ phone: filters.phone.includes(value) ? filters.phone.filter((item) => item !== value) : [...filters.phone, value] })
  return <div className="attendance-more-fields">
    <label>Check-in or check-out<select value={filters.type} onChange={(event) => onChange({ type: event.target.value as FilterValues['type'] })}><option value="both">Both</option><option value="in">Check-in</option><option value="out">Check-out</option></select></label>
    <label className="attendance-switch"><input type="checkbox" role="switch" checked={filters.by_admin} onChange={(event) => onChange({ by_admin: event.target.checked })} /> Sent by an admin</label>
    <fieldset className="attendance-phone-filter"><legend>Phone message</legend>
      {(Object.keys(PHONE_FILTERS) as PhoneFilter[]).map((value) => <label key={value} className="attendance-check"><input type="checkbox" checked={filters.phone.includes(value)} onChange={() => phone(value)} />{PHONE_STATUS_WORDS[PHONE_FILTERS[value]]}</label>)}
    </fieldset>
    <label className="attendance-switch"><input type="checkbox" role="switch" checked={filters.time === 'wrong'} onChange={(event) => onChange({ time: event.target.checked ? 'wrong' : 'any' })} /> Machine time was wrong</label>
    <label>How it arrived<select value={filters.arrived} onChange={(event) => onChange({ arrived: event.target.value as FilterValues['arrived'] })}><option value="any">Any</option><option value="live">Live</option><option value="later">Collected later</option></select></label>
    <label className="attendance-switch"><input type="checkbox" role="switch" checked={filters.no_id} onChange={(event) => onChange({ no_id: event.target.checked })} /> No CNIC</label>
    {filters.machine_match && <label className="attendance-switch"><input type="checkbox" role="switch" checked={filters.machine_match} onChange={(event) => onChange({ machine_match: event.target.checked })} /> Matched on the machine</label>}
  </div>
}

export function AttendanceFilters({ filters, onChange, search, onSearch, devices, division, counts, actions }: {
  filters: FilterValues
  onChange: (change: Partial<FilterValues>) => void
  search: string
  onSearch: (value: string) => void
  devices: Device[]
  division: DivisionChoice
  counts: GroupTotals | null
  actions?: ReactNode
}) {
  const phone = useIsPhone()
  const places = useMemo(() => placesFrom(devices).filter((place) => division === 'all' || place.division === division), [devices, division])
  const nameOf = useMemo(() => new Map(placesFrom(devices).map((place) => [place.id, place.name])), [devices])
  const cnic = cnicDigits(search)
  const placeText = filters.device.length === 0 ? 'All places' : filters.device.length === 1 ? nameOf.get(filters.device[0]) || 'Place: 1 chosen' : `Place: ${filters.device.length} chosen`
  const statusText = filters.status.length === 0 ? 'Status' : filters.status.length === 1 ? GROUP_CHIP[filters.status[0]].label : `Status: ${filters.status.length} chosen`

  const active: ActiveFilter[] = [
    ...filters.device.map((id) => ({ id: `device-${id}`, label: nameOf.get(id) || 'Place not known', onRemove: () => onChange({ device: filters.device.filter((item) => item !== id) }) })),
    ...filters.status.map((group) => ({ id: `status-${group}`, label: GROUP_CHIP[group].label, onRemove: () => onChange({ status: filters.status.filter((item) => item !== group) }) })),
    ...(filters.type !== 'both' ? [{ id: 'type', label: filters.type === 'in' ? 'Check-in' : 'Check-out', onRemove: () => onChange({ type: 'both' }) }] : []),
    ...(filters.by_admin ? [{ id: 'by_admin', label: 'Sent by an admin', onRemove: () => onChange({ by_admin: false }) }] : []),
    ...filters.phone.map((value) => ({ id: `phone-${value}`, label: `Phone message: ${PHONE_STATUS_WORDS[PHONE_FILTERS[value]].toLowerCase()}`, onRemove: () => onChange({ phone: filters.phone.filter((item) => item !== value) }) })),
    ...(filters.time === 'wrong' ? [{ id: 'time', label: 'Machine time was wrong', onRemove: () => onChange({ time: 'any' }) }] : []),
    ...(filters.arrived !== 'any' ? [{ id: 'arrived', label: filters.arrived === 'live' ? 'Live' : 'Collected later', onRemove: () => onChange({ arrived: 'any' }) }] : []),
    ...(filters.no_id ? [{ id: 'no_id', label: 'No CNIC', onRemove: () => onChange({ no_id: false }) }] : []),
    ...(filters.machine_match ? [{ id: 'machine_match', label: 'Matched on the machine', onRemove: () => onChange({ machine_match: false }) }] : []),
    ...(cnic ? [{ id: 'cnic', label: `CNIC ending …${cnic.slice(-3)}`, onRemove: () => onSearch('') }] : []),
  ]
  const more = phone
    ? <>
      <h3 className="attendance-sheet-subhead">Place</h3>
      <PlaceList places={places} chosen={filters.device} onChange={(device) => onChange({ device })} />
      <StatusList chosen={filters.status} counts={counts} onChange={(status) => onChange({ status })} />
      <MoreFields filters={filters} onChange={onChange} />
    </>
    : <MoreFields filters={filters} onChange={onChange} />
  const moreCount = moreFiltersCount(filters) + (phone ? filters.device.length + filters.status.length : 0)

  return <div className="attendance-filters">
    <FilterBar
      search={{ value: search, onChange: onSearch, label: 'Search check-ins', placeholder: 'Name, Employee ID or CNIC' }}
      moreFilters={more}
      moreFiltersCount={moreCount}
      active={active}
      onClearAll={() => { onChange(CLEARED); onSearch('') }}
      actions={<>
        <DatePicker filters={filters} onChange={onChange} />
        {!phone && <Popover buttonText={placeText} buttonLabel={`Place: ${placeText}`} title="Place">{() => <PlaceList places={places} chosen={filters.device} onChange={(device) => onChange({ device })} />}</Popover>}
        {!phone && <Popover buttonText={statusText} buttonLabel={`Status: ${statusText}`} title="Status">{() => <StatusList chosen={filters.status} counts={counts} onChange={(status) => onChange({ status })} />}</Popover>}
        {actions}
      </>}
    />
    {cnic && <p className="attendance-field-note">Searching by CNIC</p>}
  </div>
}
