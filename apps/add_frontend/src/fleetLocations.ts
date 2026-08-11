import { statusPattern, type StatusPattern } from './status'
import type { Device } from './types'

export type FleetLocationId = 'islamabad' | 'peshawar' | 'swat' | 'karachi'

export interface FleetLocationDefinition {
  id: FleetLocationId
  city: string
  region: string
  mapX: number
  mapY: number
  labelSide: 'left' | 'right'
}

export interface FleetLocationGroup {
  definition: FleetLocationDefinition
  devices: Device[]
  pattern: StatusPattern
  total: number
  online: number
  attention: number
  lastSeenAt: string | null
}

export interface FleetLocationResult {
  groups: FleetLocationGroup[]
  unmapped: Device[]
}

export const fleetLocationDefinitions: FleetLocationDefinition[] = [
  { id: 'swat', city: 'Swat', region: 'Khyber Pakhtunkhwa', mapX: 66, mapY: 17, labelSide: 'right' },
  { id: 'peshawar', city: 'Peshawar', region: 'Khyber Pakhtunkhwa', mapX: 55, mapY: 27, labelSide: 'left' },
  { id: 'islamabad', city: 'Islamabad', region: 'Islamabad Capital Territory', mapX: 79, mapY: 31, labelSide: 'left' },
  { id: 'karachi', city: 'Karachi', region: 'Sindh', mapX: 39, mapY: 87, labelSide: 'right' },
]

const definitionById = new Map(fleetLocationDefinitions.map((definition) => [definition.id, definition]))
const patternRank: Record<StatusPattern, number> = { confirmed: 0, notice: 1, waiting: 2, blocked: 3 }

export const normalizeFleetLocationText = (device: Pick<Device, 'zone_id' | 'zone_name' | 'display_name'>) =>
  `${device.zone_id} ${device.zone_name} ${device.display_name}`
    .normalize('NFKD')
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, ' ')
    .trim()

export function resolveFleetLocation(device: Pick<Device, 'zone_id' | 'zone_name' | 'display_name'>): FleetLocationDefinition | null {
  const value = ` ${normalizeFleetLocationText(device)} `
  if (/\bSLICTOWER\b|\bSLIC\s+TOWER\b/.test(value)) return definitionById.get('islamabad') || null
  if (/\bKARACHI\b/.test(value)) return definitionById.get('karachi') || null
  if (/\bPESHAWAR\b|\bPESH\b/.test(value)) return definitionById.get('peshawar') || null
  if (/\bSWAT\b/.test(value)) return definitionById.get('swat') || null
  return null
}

const aggregatePattern = (devices: Device[]) => devices.reduce<StatusPattern>((worst, device) => {
  const current = statusPattern(device.state)
  return patternRank[current] > patternRank[worst] ? current : worst
}, 'confirmed')

const newestContact = (devices: Device[]) => devices.reduce<string | null>((latest, device) => {
  if (!device.last_seen_at) return latest
  if (!latest || +new Date(device.last_seen_at) > +new Date(latest)) return device.last_seen_at
  return latest
}, null)

export function groupFleetLocations(devices: Device[]): FleetLocationResult {
  const grouped = new Map<FleetLocationId, Device[]>()
  const unmapped: Device[] = []
  for (const device of devices) {
    const definition = resolveFleetLocation(device)
    if (!definition) {
      unmapped.push(device)
      continue
    }
    const rows = grouped.get(definition.id) || []
    rows.push(device)
    grouped.set(definition.id, rows)
  }

  const groups = fleetLocationDefinitions.flatMap((definition) => {
    const rows = grouped.get(definition.id)
    if (!rows?.length) return []
    const pattern = aggregatePattern(rows)
    return [{
      definition,
      devices: rows,
      pattern,
      total: rows.length,
      online: rows.filter((device) => statusPattern(device.state) === 'confirmed').length,
      attention: rows.filter((device) => statusPattern(device.state) === 'blocked' || statusPattern(device.state) === 'waiting').length,
      lastSeenAt: newestContact(rows),
    }]
  })

  return { groups, unmapped }
}
