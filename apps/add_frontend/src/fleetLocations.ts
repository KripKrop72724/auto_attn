import { statusPattern, type StatusPattern } from './status'
import type { Device } from './types'

export type FleetLocationId = 'islamabad' | 'peshawar' | 'swat' | 'karachi'

export interface FleetLocationDefinition {
  id: FleetLocationId
  city: string
  region: string
  latitude: number
  longitude: number
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

// The Natural Earth geometry was projected into a 600 × 720 source plane. The
// displayed SVG crops that plane to `0 72 600 580`, then fits it into a square
// stage. Model both transforms so markers remain tied to latitude/longitude.
const fleetMapSourceProjection = {
  west: 60.083741,
  east: 77.821388,
  north: 39.568396,
  south: 21.183557,
  width: 600,
  height: 720,
} as const

const fleetMapDisplayViewBox = {
  x: 0,
  y: 72,
  width: 600,
  height: 580,
} as const

export function projectFleetCoordinates(latitude: number, longitude: number) {
  const svgX = ((longitude - fleetMapSourceProjection.west) / (fleetMapSourceProjection.east - fleetMapSourceProjection.west)) * fleetMapSourceProjection.width
  const svgY = ((fleetMapSourceProjection.north - latitude) / (fleetMapSourceProjection.north - fleetMapSourceProjection.south)) * fleetMapSourceProjection.height
  const displayScale = Math.min(1 / fleetMapDisplayViewBox.width, 1 / fleetMapDisplayViewBox.height)
  const displayOffsetX = (1 - fleetMapDisplayViewBox.width * displayScale) / 2
  const displayOffsetY = (1 - fleetMapDisplayViewBox.height * displayScale) / 2
  return {
    mapX: (displayOffsetX + (svgX - fleetMapDisplayViewBox.x) * displayScale) * 100,
    mapY: (displayOffsetY + (svgY - fleetMapDisplayViewBox.y) * displayScale) * 100,
  }
}

const defineFleetLocation = (
  definition: Omit<FleetLocationDefinition, 'mapX' | 'mapY'>,
): FleetLocationDefinition => ({ ...definition, ...projectFleetCoordinates(definition.latitude, definition.longitude) })

export const fleetLocationDefinitions: FleetLocationDefinition[] = [
  defineFleetLocation({ id: 'swat', city: 'Swat', region: 'Khyber Pakhtunkhwa', latitude: 34.7717, longitude: 72.3602, labelSide: 'right' }),
  defineFleetLocation({ id: 'peshawar', city: 'Peshawar', region: 'Khyber Pakhtunkhwa', latitude: 34.0151, longitude: 71.5249, labelSide: 'left' }),
  defineFleetLocation({ id: 'islamabad', city: 'Islamabad', region: 'Islamabad Capital Territory', latitude: 33.6844, longitude: 73.0479, labelSide: 'left' }),
  defineFleetLocation({ id: 'karachi', city: 'Karachi', region: 'Sindh', latitude: 24.8607, longitude: 67.0011, labelSide: 'right' }),
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
  if (/\bSLICTOWER\b|\bSLIC\s+TOWER\b|\bBLD\d*ISB\b|\bISLAMABAD\b/.test(value)) return definitionById.get('islamabad') || null
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
