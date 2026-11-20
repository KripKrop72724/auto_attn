// Friendly device names and the Life / Health rule, mirrored from the backend
// (zk_add/public_status.py `device_name` and `build_snapshot`, the source of
// truth). The dashboard uses the backend's `division` / `friendly_name` fields
// when a device row carries them; this mirror is only the fallback for an older
// backend. deviceNames.fixture.json holds the shared vectors: pytest checks the
// backend against it and vitest checks this file, so the two cannot drift.

export type DivisionKey = 'life' | 'health'

export interface DeviceName {
  zone: DivisionKey
  location: string
  /** "Device 3" or "13th Floor". */
  unit: string | null
  /** A numbered unit is named only when its location has several. */
  number: number | null
}

const LOCATIONS: Record<string, string> = { SLICTOWER: 'SLIC Tower' }

const ordinal = (value: number) => {
  const suffix = value % 100 >= 10 && value % 100 <= 20
    ? 'th'
    : ({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string>)[value % 10] || 'th'
  return `${value}${suffix}`
}

// Python's str.capitalize(): first character upper, the rest lower.
const capitalize = (word: string) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()

function location(code: string): string {
  const compact = code.replace(/[\s_-]+/g, '')
  if (LOCATIONS[compact]) return LOCATIONS[compact]
  const building = /^(G&?P)?BLD(\d+)$/.exec(compact)
  if (building) {
    const name = `Building ${Number.parseInt(building[2], 10)}`
    return building[1] ? `Group & Pension ${name}` : name
  }
  return code.split(/[\s_-]+/).filter(Boolean).map(capitalize).join(' ')
}

/** The place, unit and division a device code reads as ("LF-ZONE-BLD9-03" → Life, Building 9, Device 3). */
export function deviceName(code: string | null | undefined): DeviceName {
  const original = (code || '').trim()
  const value = original.toUpperCase()
  const life = /^LF[\s_-]+/.exec(value)
  const zone: DivisionKey = life ? 'life' : 'health'
  const rest = (life ? value.slice(life[0].length) : value).replace(/^ZONE[\s_-]+/, '')
  const floor = /^(.+?)[\s_-]+(\d{1,3})\s*FL$/.exec(rest)
  if (floor) return { zone, location: location(floor[1]), unit: `${ordinal(Number.parseInt(floor[2], 10))} Floor`, number: null }
  const numbered = /^(.+?)[\s_-]+(\d{1,3})$/.exec(rest)
  if (numbered) {
    const number = Number.parseInt(numbered[2], 10)
    return { zone, location: location(numbered[1]), unit: `Device ${number}`, number }
  }
  return { zone, location: location(rest) || original, unit: null, number: null }
}

type NaturalPart = string | number

const natural = (text: string): NaturalPart[] =>
  text.split(/(\d+)/).map((part) => (/^\d+$/.test(part) ? Number.parseInt(part, 10) : part.toLowerCase()))

function compareNatural(left: NaturalPart[], right: NaturalPart[]) {
  const length = Math.min(left.length, right.length)
  for (let index = 0; index < length; index += 1) {
    const a = left[index]
    const b = right[index]
    if (a === b) continue
    if (typeof a === 'number' && typeof b === 'number') return a - b
    return String(a) < String(b) ? -1 : 1
  }
  return left.length - right.length
}

export interface NamedDevice {
  code: string
  zone: DivisionKey
  /** "Building 9 · Device 3", or just "Swat" when the place has one device. */
  name: string
  location: string
  unit: string | null
}

/**
 * Friendly names for a set of devices, in the /status order (Life first, then by
 * place and unit). A device number is shown only where a place has several.
 */
export function friendlyNames(codes: readonly string[]): NamedDevice[] {
  const named = codes.map((code) => ({ code, name: deviceName(code) }))
  const numbered = new Map<string, number>()
  for (const { name } of named) {
    if (name.number === null) continue
    const key = `${name.zone}\u0000${name.location}`
    numbered.set(key, (numbered.get(key) || 0) + 1)
  }
  const zoneRank: Record<DivisionKey, number> = { life: 0, health: 1 }
  return [...named]
    .sort((left, right) =>
      zoneRank[left.name.zone] - zoneRank[right.name.zone]
      || compareNatural(natural(left.name.location), natural(right.name.location))
      || compareNatural(natural(left.name.unit || ''), natural(right.name.unit || ''))
      || (left.code < right.code ? -1 : left.code > right.code ? 1 : 0))
    .map(({ code, name }) => {
      const shown = Boolean(name.unit) && (name.number === null || (numbered.get(`${name.zone}\u0000${name.location}`) || 0) > 1)
      return {
        code,
        zone: name.zone,
        name: shown ? `${name.location} · ${name.unit}` : name.location,
        location: name.location,
        unit: shown ? name.unit : null,
      }
    })
}
