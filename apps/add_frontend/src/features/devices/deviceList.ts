// The device rows Home and Devices show, and the Devices filters (03-home-devices
// "Filters"). Everything runs in the browser over the device store, so Home's
// tiles always equal the Devices chips: both count the same rows the same way.
import { deviceName, friendlyNames, type DivisionKey } from '../../lib/deviceNames'
import { deviceDivision } from '../../lib/division'
import type { DivisionChoice } from '../../lib/prefs'
import type { Device } from '../../types'
import { deviceStatus, type DeviceStatus, type DeviceStatusGroup, type PublicStatusField } from '../../words/deviceStatus'
import type { Tone as ChipTone } from '../../ui/StatusChip'

/** Optional read-side fields of a row from a newer backend (HOME-2). */
type NamedRow = Device & {
  place?: string | null
  unit?: string | null
  city?: string | null
  public_status?: PublicStatusField | null
}

export type Brand = 'zkteco' | 'hikvision'
export type StatusFilter = 'all' | 'attention' | 'offline' | 'working' | 'setup'
export type SortKey = 'attention' | 'name' | 'seen'

export interface DeviceView {
  id: string
  device: Device
  name: string
  division: DivisionKey
  city: string
  place: string
  brand: Brand | null
  /** "2.8.2", or null when not known. */
  version: string | null
  status: DeviceStatus
  spare: boolean
  /** Lower-case text the search box looks through. */
  haystack: string
}

/** City of a place (be/device_names.py CITY_BY_PLACE; an older backend has no `city`). */
export function cityOf(place: string): string {
  if (/^(Group & Pension )?Building \d+$/.test(place)) return 'Karachi'
  if (place === 'SLIC Tower') return 'Islamabad'
  return place
}

export const chipTone = (group: DeviceStatusGroup): ChipTone =>
  group === 'working' ? 'working' : group === 'attention' ? 'attention' : group === 'offline' ? 'offline' : 'neutral'

/** The software version without its build prefix ("zone-lite-2.8.2" → "2.8.2"). */
export function softwareVersion(value: string | null | undefined): string | null {
  const text = (value || '').trim().replace(/^zone-lite-/i, '')
  return text || null
}

export function deviceBrand(device: Device): Brand | null {
  const vendor = device.terminal_vendor || device.firmware_family || (device.hikvision ? 'hikvision' : device.zkt ? 'zkt' : null)
  if (vendor === 'hikvision') return 'hikvision'
  if (vendor === 'zkt') return 'zkteco'
  return null
}

export const BRAND_WORD: Record<Brand, string> = { zkteco: 'ZKTeco', hikvision: 'Hikvision' }

const deviceCode = (device: Device) => device.display_name || device.zone_id || ''

/**
 * The rows with their names and plain status. Names come from the backend's
 * `friendly_name`; for an older backend the browser mirror numbers a unit only
 * where its place has several active devices (the /status rule).
 */
export function deviceViews(devices: readonly Device[], now: number = Date.now()): DeviceView[] {
  void now
  const fallback = new Map<string, string>()
  if (devices.some((row) => !row.friendly_name)) {
    const active = devices.filter((row) => !row.is_spare)
    for (const named of friendlyNames(active.map(deviceCode))) fallback.set(named.code, named.name)
  }
  return devices.map((device) => {
    const row = device as NamedRow
    const parsed = deviceName(deviceCode(device))
    const place = row.place || parsed.location
    const fallbackName = device.is_spare
      ? `${parsed.unit && parsed.number === null ? `${parsed.location} · ${parsed.unit}` : parsed.location} · Spare`
      : fallback.get(deviceCode(device)) || parsed.location
    const name = row.friendly_name?.trim() || fallbackName || device.display_name || device.connector_id
    const division = deviceDivision(device)
    const city = row.city || cityOf(place)
    const brand = deviceBrand(device)
    const version = softwareVersion(device.firmware_version)
    const status = deviceStatus(row)
    const haystack = [
      name, city, place, device.display_name, device.zone_id, device.zone_name, device.hardware_id,
      device.zkt?.serial, division, brand ? BRAND_WORD[brand] : '',
    ].filter(Boolean).join(' ').toLowerCase()
    return { id: device.connector_id, device, name, division, city, place, brand, version, status, spare: Boolean(device.is_spare), haystack }
  })
}

const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' })
export const compareNames = (left: DeviceView, right: DeviceView) =>
  collator.compare(left.name, right.name) || (left.id < right.id ? -1 : left.id > right.id ? 1 : 0)

/** Rank for "Needs attention first": Offline 0, Needs attention 1, Setting up 2, Working 3. */
export const statusRank = (view: DeviceView) => ({ offline: 0, attention: 1, setup: 2, working: 3, spare: 4 })[view.status.group]

export function sortViews(views: readonly DeviceView[], sort: SortKey): DeviceView[] {
  const list = [...views]
  if (sort === 'name') return list.sort(compareNames)
  if (sort === 'seen') {
    const seen = (view: DeviceView) => (view.device.last_seen_at ? Date.parse(view.device.last_seen_at) : Number.NEGATIVE_INFINITY)
    return list.sort((left, right) => seen(left) - seen(right) || compareNames(left, right))
  }
  return list.sort((left, right) => statusRank(left) - statusRank(right) || compareNames(left, right))
}

export interface DeviceFilters {
  division: DivisionChoice
  status: StatusFilter
  cities: string[]
  brand: Brand | 'all'
  spare: boolean
  version: string
  note: boolean
  q: string
}

export const DEFAULT_FILTERS: DeviceFilters = {
  division: 'all', status: 'all', cities: [], brand: 'all', spare: false, version: '', note: false, q: '',
}

type FilterKey = 'division' | 'status' | 'city' | 'brand' | 'spare' | 'version' | 'note' | 'q'

/** True when the row passes every filter except the one named in `skip` (for chip counts). */
export function passes(view: DeviceView, filters: DeviceFilters, skip?: FilterKey): boolean {
  if (skip !== 'division' && filters.division !== 'all' && view.division !== filters.division) return false
  if (skip !== 'spare') {
    if (filters.spare !== view.spare) return false
  }
  if (!filters.spare && skip !== 'status' && filters.status !== 'all' && view.status.group !== filters.status) return false
  if (!filters.spare && skip !== 'note' && filters.note && !view.status.note) return false
  if (skip !== 'city' && filters.cities.length && !filters.cities.includes(view.city)) return false
  if (skip !== 'brand' && filters.brand !== 'all' && view.brand !== filters.brand) return false
  if (skip !== 'version' && filters.version) {
    if (filters.version === 'unknown' ? view.version !== null : view.version !== filters.version) return false
  }
  if (skip !== 'q' && filters.q.trim()) {
    const words = filters.q.trim().toLowerCase().split(/\s+/)
    if (!words.every((word) => view.haystack.includes(word))) return false
  }
  return true
}

export const applyFilters = (views: readonly DeviceView[], filters: DeviceFilters) => views.filter((view) => passes(view, filters))

/** Status chip counts over the rows that pass every other filter. */
export function statusCounts(views: readonly DeviceView[], filters: DeviceFilters): Record<StatusFilter, number> {
  const counts: Record<StatusFilter, number> = { all: 0, attention: 0, offline: 0, working: 0, setup: 0 }
  for (const view of views) {
    if (view.spare || !passes(view, { ...filters, spare: false, note: false }, 'status')) continue
    counts.all += 1
    if (view.status.group !== 'spare') counts[view.status.group] += 1
  }
  return counts
}

export const spareCount = (views: readonly DeviceView[], filters: DeviceFilters) =>
  views.filter((view) => passes(view, { ...filters, spare: true, status: 'all', note: false }, 'spare') && view.spare).length

/** Cities present, with how many rows each has under the other filters. */
export function cityOptions(views: readonly DeviceView[], filters: DeviceFilters): Array<{ city: string; count: number }> {
  const counts = new Map<string, number>()
  for (const view of views) {
    if (!passes(view, filters, 'city')) continue
    counts.set(view.city, (counts.get(view.city) || 0) + 1)
  }
  for (const city of filters.cities) if (!counts.has(city)) counts.set(city, 0)
  return [...counts.entries()].map(([city, count]) => ({ city, count })).sort((a, b) => collator.compare(a.city, b.city))
}

const versionParts = (value: string) => value.split(/[.\-]/).map((part) => Number.parseInt(part, 10) || 0)
const newerFirst = (left: string, right: string) => {
  const a = versionParts(left)
  const b = versionParts(right)
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const diff = (b[index] || 0) - (a[index] || 0)
    if (diff) return diff
  }
  return 0
}

/** Software versions present: ZKTeco link boxes first (newest first), then Hikvision ones. */
export function versionOptions(views: readonly DeviceView[], filters: DeviceFilters): Array<{ id: string; version: string | null; count: number; hikvision: boolean }> {
  const counts = new Map<string, { count: number; hikvision: boolean }>()
  let unknown = 0
  for (const view of views) {
    if (!passes(view, filters, 'version')) continue
    if (!view.version) { unknown += 1; continue }
    const entry = counts.get(view.version) || { count: 0, hikvision: view.brand === 'hikvision' }
    entry.count += 1
    counts.set(view.version, entry)
  }
  const rows = [...counts.entries()].map(([version, entry]) => ({ id: version, version, count: entry.count, hikvision: entry.hikvision }))
    .sort((a, b) => Number(a.hikvision) - Number(b.hikvision) || newerFirst(a.version, b.version))
  return unknown ? [...rows, { id: 'unknown', version: null, count: unknown, hikvision: false }] : rows
}

/** Home counts (non-spare rows only), per division and in total. */
export interface FleetCounts {
  total: number
  working: number
  withNote: number
  attention: number
  offline: number
  setup: number
  spares: number
}

export function fleetCounts(views: readonly DeviceView[]): FleetCounts {
  const counts: FleetCounts = { total: 0, working: 0, withNote: 0, attention: 0, offline: 0, setup: 0, spares: 0 }
  for (const view of views) {
    if (view.spare) { counts.spares += 1; continue }
    const group = view.status.group
    if (group === 'setup') { counts.setup += 1; continue }
    counts.total += 1
    if (group === 'working') {
      counts.working += 1
      if (view.status.note) counts.withNote += 1
    } else if (group === 'attention') counts.attention += 1
    else if (group === 'offline') counts.offline += 1
  }
  return counts
}

/** Home panel rows: everything not Working, worst first (Offline, Needs attention, Setting up), then longest first. */
export function notWorking(views: readonly DeviceView[]): DeviceView[] {
  const since = (view: DeviceView) => {
    const value = view.status.since || view.device.last_seen_at
    const parsed = value ? Date.parse(value) : Number.NaN
    return Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY
  }
  return views
    .filter((view) => !view.spare && view.status.group !== 'working')
    .sort((left, right) => statusRank(left) - statusRank(right) || since(left) - since(right) || compareNames(left, right))
}
