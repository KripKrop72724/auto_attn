import { useSyncExternalStore } from 'react'
import type { Device } from './types'

/**
 * Combines a stored row with a newer one. A slim list row (`payload: 'list'`)
 * leaves out heavy detail fields, so those stay from the detail row; the
 * machine (`zkt`) block merges field by field, and a slim row's capabilities add
 * to the known ones instead of replacing them. A full row replaces everything.
 */
export function mergeDevice(prior: Device | undefined, row: Device): Device {
  if (!prior) return row
  const slim = row.payload === 'list'
  const merged: Device = { ...prior, ...row }
  if (row.zkt && prior.zkt) {
    const zkt = { ...prior.zkt, ...row.zkt }
    if (slim) {
      zkt.capabilities = { ...(prior.zkt.capabilities || {}), ...(row.zkt.capabilities || {}) }
      if (row.zkt.certification_observations === undefined) zkt.certification_observations = prior.zkt.certification_observations
    }
    merged.zkt = zkt
  }
  if (slim) {
    if (row.firmware_diagnostics == null && prior.firmware_diagnostics != null) {
      merged.firmware_diagnostics = prior.firmware_diagnostics
      merged.firmware_diagnostics_at = prior.firmware_diagnostics_at
    }
    // The row still carries the detail fields kept above.
    if (prior.payload !== 'list') merged.payload = prior.payload
  }
  return merged
}

// Fleet and drawer use the same immutable snapshots. The server capture time
// orders concurrent list/detail responses, independently of response arrival.
export class DeviceSnapshots {
  private rows = new Map<string, Device>()
  private fleetIds: string[] = []
  private list: Device[] = []
  private loadedFlag = false
  private listeners = new Set<() => void>()
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  get = (id: string) => this.rows.get(id)
  all = () => this.list
  /** True once a whole device list has arrived: before that, "no devices" is not known yet. */
  loaded = () => this.loadedFlag
  replaceFleet = (incoming: Device[]) => {
    this.loadedFlag = true
    this.put(incoming, true)
  }
  put = (incoming: Device[], replaceFleet = false) => {
    let changed = replaceFleet
    if (replaceFleet) this.fleetIds = incoming.map(row => row.connector_id)
    for (const row of incoming) {
      if (!row || typeof row.connector_id !== 'string') continue
      const prior = this.rows.get(row.connector_id)
      // Older live payloads wrote dates with a space ("2026-11-20 10:00:00+00:00"); Safari needs the T.
      const revision = (value: Device) => Date.parse((value.snapshot_at || value.firmware_diagnostics_at || value.last_seen_at || '').replace(/^(\d{4}-\d{2}-\d{2}) /, '$1T'))
      if (prior && Number.isFinite(revision(prior)) && (!Number.isFinite(revision(row)) || revision(row) < revision(prior))) continue
      this.rows.set(row.connector_id, mergeDevice(prior, row))
      changed = true
    }
    if (changed) this.notify()
  }
  clear = () => { this.rows.clear(); this.fleetIds = []; this.loadedFlag = false; this.notify() }
  private notify = () => {
    this.list = this.fleetIds.map(id => this.rows.get(id)!).filter(Boolean)
    this.listeners.forEach(listener => listener())
  }
}

export const deviceSnapshots = new DeviceSnapshots()
export const useDevices = () => useSyncExternalStore(deviceSnapshots.subscribe, deviceSnapshots.all)
export const useDevicesLoaded = () => useSyncExternalStore(deviceSnapshots.subscribe, deviceSnapshots.loaded)
export const useDevice = (seed: Device) => useSyncExternalStore(deviceSnapshots.subscribe, () => deviceSnapshots.get(seed.connector_id) || seed)

/**
 * A `device` live event that is a whole device row (heartbeats carry one) can
 * update the list in place. Partial events (`{connector_id, connected}`,
 * `{connector_id, is_spare}`, a COMM Key or binding state) need the list re-read.
 */
export function isFullDeviceRow(data: unknown): data is Device {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return false
  const row = data as Record<string, unknown>
  return typeof row.connector_id === 'string' && typeof row.snapshot_at === 'string'
    && Boolean(row.health && typeof row.health === 'object')
    && typeof row.display_name === 'string' && typeof row.state === 'string'
}
