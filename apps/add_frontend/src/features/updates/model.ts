// Software updates (08-advanced 2): shared data helpers for the page parts.
// Reads only; every action keeps its exact request body (one click each).
import { deviceDivision, type DivisionKey } from '../../lib/division'
import { deviceName, friendlyNames } from '../../lib/deviceNames'
import type { Device, FirmwareCampaign, FirmwareCampaignTotals, FirmwareDeployment, FirmwareListResponse, FirmwareRelease, FirmwareReleaseTotals } from '../../types'
import { compareVersions, plainVersion, type LiveDeployment } from '../../words/software'
import { hilAllowedTargets, hilDevice } from '../hilTargets'

/** Campaign rows carry plain fields from the list endpoint (ADV-17); older servers omit them. */
export type UpdateRow = FirmwareCampaign & {
  place_name?: string | null
  division?: 'life' | 'health' | null
  pause_kind?: string | null
  pause_code?: string | null
  pause_device_name?: string | null
  pause_device_connector_id?: string | null
}

export type CampaignResponse = FirmwareListResponse<UpdateRow, FirmwareCampaignTotals>
export type ReleaseResponse = FirmwareListResponse<FirmwareRelease, FirmwareReleaseTotals>

export interface Named {
  name: string
  division: DivisionKey
}

/** Friendly names for every device (backend `friendly_name` first, the /status rule as fallback). */
export function nameIndex(devices: Device[]) {
  const active = devices.filter((device) => !device.is_spare)
  const fallback = new Map(friendlyNames(active.map((device) => device.display_name || device.zone_id)).map((row) => [row.code, row.name]))
  const byConnector = new Map<string, Named>()
  const byZone = new Map<string, Named>()
  for (const device of devices) {
    const code = device.display_name || device.zone_id
    const name = device.friendly_name || fallback.get(code) || deviceName(code).location || code
    const named = { name, division: deviceDivision(device) }
    byConnector.set(device.connector_id, named)
    if (!device.is_spare && device.zone_id && !byZone.has(device.zone_id)) byZone.set(device.zone_id, named)
  }
  return {
    device: (id: string | null | undefined): Named | null => (id ? byConnector.get(id) ?? null : null),
    zone: (zoneId: string | null | undefined): Named => {
      if (zoneId && byZone.has(zoneId)) return byZone.get(zoneId)!
      const parsed = deviceName(zoneId || '')
      return { name: parsed.unit ? `${parsed.location} · ${parsed.unit}` : parsed.location || 'Unknown place', division: parsed.zone }
    },
  }
}
export type NameIndex = ReturnType<typeof nameIndex>

/** The place of an update: the row's own plain fields first. */
export function updatePlace(row: UpdateRow, names: NameIndex): Named {
  const zone = names.zone(row.zone_id)
  return {
    name: row.place_name || zone.name,
    division: row.division === 'life' || row.division === 'health' ? row.division : zone.division,
  }
}

export const LIVE_STATES = ['ACTIVE', 'PAUSED']
export const CLOSED_STATES = ['CANCELLED', 'COMPLETED']

/** Flattens the deployments of live updates for softwareStatus(). */
export function liveDeployments(campaigns: UpdateRow[], releases: FirmwareRelease[]): LiveDeployment[] {
  const sizes = new Map(releases.map((release) => [release.release_id, release.image_size]))
  return campaigns
    .filter((campaign) => LIVE_STATES.includes(campaign.status))
    .flatMap((campaign) => (campaign.deployments ?? []).map((deployment: FirmwareDeployment) => ({
      connector_id: deployment.connector_id,
      status: deployment.status,
      target_version: deployment.target_version,
      previous_version: deployment.previous_version,
      bytes_written: deployment.bytes_written,
      error_code: deployment.error_code,
      image_size: (campaign.release_id && sizes.get(campaign.release_id)) || null,
      update_id: campaign.campaign_id,
      updated_at: deployment.updated_at,
    })))
}

/** Test versions made for one exact device (hilTargets exact match). */
export function testVersions(releases: FirmwareRelease[], devices: Device[]): Map<string, string[]> {
  const result = new Map<string, string[]>()
  for (const release of releases) {
    if (release.state !== 'HIL_ONLY') continue
    const targets = release.hil_targets ? hilAllowedTargets(release).map((target) => target.connector_id) : [hilDevice(release, devices)?.connector_id]
    for (const id of targets) {
      if (!id) continue
      result.set(id, [...(result.get(id) ?? []), release.version])
    }
  }
  return result
}

/** Versions a person may start now (the switches decide). */
export const publishable = (release: FirmwareRelease, enabled: boolean, hilEnabled: boolean) =>
  release.state === 'AVAILABLE' ? enabled : release.state === 'HIL_ONLY' && hilEnabled

export const isCurrentRelease = (release: FirmwareRelease) => release.current !== false

export const releaseFamily = (release: Pick<FirmwareRelease, 'firmware_family' | 'release_id'>): 'zkt' | 'hikvision' =>
  release.firmware_family === 'hikvision' || release.release_id?.includes('hikvision') ? 'hikvision' : 'zkt'

/** Devices (not spares) that run exactly this version, split Life / Health. */
export function devicesOnVersion(release: FirmwareRelease, devices: Device[]) {
  const family = releaseFamily(release)
  const running = devices.filter((device) => !device.is_spare && (device.firmware_family ?? 'zkt') === family &&
    compareVersions(device.firmware_version, release.version) === 0 && plainVersion(device.firmware_version) !== null)
  return {
    total: running.length,
    life: running.filter((device) => deviceDivision(device) === 'life').length,
    health: running.filter((device) => deviceDivision(device) === 'health').length,
  }
}

export const isAbort = (reason: unknown) => reason instanceof DOMException && reason.name === 'AbortError'

/** Calls the function every `ms` while `active` and the tab is visible. */
export function visibleInterval(active: boolean, ms: number, run: () => void): () => void {
  if (!active) return () => undefined
  const timer = window.setInterval(() => {
    if (typeof document !== 'undefined' && document.hidden) return
    run()
  }, ms)
  return () => window.clearInterval(timer)
}

/** "82.1 MB" / "300 KB", like the computer shows it. */
export function fileSize(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
