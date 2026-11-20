// Friendly device names on Recover attendance. The backend's `friendly_name`
// and `division` win (HOME-2); the mirror in lib/deviceNames is only the
// fallback for an older backend, worked out against every known device so a
// number is shown only where a place has several devices.
import { deviceName, friendlyNames, type DivisionKey } from '../../lib/deviceNames'
import type { Device } from '../../types'

export interface Place {
  /** "Building 9 · Device 3". */
  name: string
  division: DivisionKey
  /** "Building 9", "Multan". */
  city: string
  /** The device code, kept for the `title` tooltip and Technical details only. */
  code: string
}

export interface NameSource {
  connector_id?: string | null
  display_name?: string | null
  zone_id?: string | null
  friendly_name?: string | null
  division?: string | null
}

export type Namer = (source: NameSource | null | undefined) => Place

export function makeNamer(devices: readonly Device[]): Namer {
  const codes = [...new Set(devices.map((device) => device.display_name || device.zone_id).filter(Boolean))]
  const mirror = new Map(friendlyNames(codes).map((named) => [named.code, named]))
  return (source) => {
    const code = source?.display_name || source?.zone_id || source?.connector_id || ''
    const parsed = deviceName(code)
    const division: DivisionKey = source?.division === 'life' || source?.division === 'health' ? source.division : parsed.zone
    if (source?.friendly_name) return { name: source.friendly_name, division, city: parsed.location, code }
    const known = mirror.get(code) || friendlyNames([...codes, code]).find((named) => named.code === code)
    return { name: known?.name || code || 'Unknown device', division, city: parsed.location, code }
  }
}

/** A device row's place (its own friendly fields first). */
export const devicePlace = (namer: Namer, device: Device): Place => namer(device)
