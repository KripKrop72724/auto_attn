import { useSyncExternalStore } from 'react'
import type { Device } from './types'

// Fleet and drawer use the same immutable snapshots. The server capture time
// orders concurrent list/detail responses, independently of response arrival.
export class DeviceSnapshots {
  private rows = new Map<string, Device>()
  private fleetIds: string[] = []
  private list: Device[] = []
  private listeners = new Set<() => void>()
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  get = (id: string) => this.rows.get(id)
  all = () => this.list
  replaceFleet = (incoming: Device[]) => this.put(incoming, true)
  put = (incoming: Device[], replaceFleet = false) => {
    let changed = replaceFleet
    if (replaceFleet) this.fleetIds = incoming.map(row => row.connector_id)
    for (const row of incoming) {
      const prior = this.rows.get(row.connector_id)
      const revision = (value: Device) => Date.parse(value.snapshot_at || value.firmware_diagnostics_at || value.last_seen_at || '')
      if (prior && Number.isFinite(revision(prior)) && (!Number.isFinite(revision(row)) || revision(row) < revision(prior))) continue
      this.rows.set(row.connector_id, { ...prior, ...row })
      changed = true
    }
    if (changed) this.notify()
  }
  clear = () => { this.rows.clear(); this.fleetIds = []; this.notify() }
  private notify = () => {
    this.list = this.fleetIds.map(id => this.rows.get(id)!).filter(Boolean)
    this.listeners.forEach(listener => listener())
  }
}

export const deviceSnapshots = new DeviceSnapshots()
export const useDevices = () => useSyncExternalStore(deviceSnapshots.subscribe, deviceSnapshots.all)
export const useDevice = (seed: Device) => useSyncExternalStore(deviceSnapshots.subscribe, () => deviceSnapshots.get(seed.connector_id) || seed)
