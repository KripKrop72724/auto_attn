// Life and Health. One rule, one source: the backend's `division` field on a
// device row (HOME-2); the name rule in deviceNames.ts is only the fallback for
// an older backend. Spares keep their division but never count in totals.
import { deviceName, type DivisionKey } from './deviceNames'
import type { DivisionChoice } from './prefs'

export type { DivisionKey } from './deviceNames'
export type { DivisionChoice } from './prefs'

export const DIVISIONS: readonly DivisionKey[] = ['life', 'health']

export const divisionLabel: Record<DivisionChoice, string> = { all: 'All', life: 'Life', health: 'Health' }

interface DivisionSource {
  division?: string | null
  display_name?: string | null
  zone_id?: string | null
}

/** Life or Health for a device row. */
export function deviceDivision(device: DivisionSource): DivisionKey {
  if (device.division === 'life' || device.division === 'health') return device.division
  return deviceName(device.display_name || device.zone_id || '').zone
}

/** True when the device belongs to the chosen division ("all" keeps every device). */
export const inDivision = (device: DivisionSource, choice: DivisionChoice) =>
  choice === 'all' || deviceDivision(device) === choice

/** The `division` URL value: "life" or "health"; anything else (or nothing) means All. */
export function parseDivision(search: string | URLSearchParams): DivisionChoice {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search
  const value = params.get('division')
  return value === 'life' || value === 'health' ? value : 'all'
}
