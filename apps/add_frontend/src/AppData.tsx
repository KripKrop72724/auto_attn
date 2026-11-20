// What every page can read from the app frame: the live device list, the
// overview, live-update revisions, the toast and the signed-in name. Pages are
// mounted by small route modules (src/routes/*) that read this and pass the
// same props each page takes, so a page change never needs App.tsx.
import { createContext, useContext } from 'react'
import type { RealtimeState, RealtimeTopic } from './realtime'
import type { Device, Overview } from './types'
import type { Toast } from './ui/Toast'

export type Revisions = Record<RealtimeTopic, number>

export interface AppData {
  username: string
  /** Every device in the fleet list order; empty until the first answer. */
  devices: Device[]
  /** False until the first device list answer (or error): show a loading state, never "0". */
  devicesLoaded: boolean
  /** The last background refresh failed; the list shows what we knew. */
  devicesError: unknown
  /** When the device list last arrived. */
  devicesUpdatedAt: Date | null
  /** Null until the first overview answer: never show its counts as 0 before that. */
  overview: Overview | null
  /** True while the first device list is loading. */
  loading: boolean
  revisions: Revisions
  /**
   * Live changes for one device (its heartbeats, logs and commands), plus events
   * that do not say which device they are for and every resync. An open device
   * reloads for these only, not for every other device's heartbeat.
   */
  deviceRevision: (connectorId: string) => number
  /** Check-ins of one device plus recovery events and resyncs. */
  deviceCustodyRevision: (connectorId: string) => number
  realtime: { state: RealtimeState; lastSyncAt: Date | null }
  toast: Toast
  /** Re-read devices and overview now (after an action, or "Try now"). */
  refreshFleet: () => Promise<void>
  /** People page: the machine picked in `/people/:deviceId`. */
  selectedDeviceId: string
  selectUserDevice: (id: string) => void
  /** Opens one device; `list` (connector ids in the order shown) turns on Previous / Next. */
  inspectDevice: (device: Device, list?: readonly string[]) => void
  manageUsers: (device: Device) => void
  navigate: (to: string) => void
  /** Sign out (Settings, account menu); resolves when the session has ended. */
  logout: () => Promise<void>
}

const AppDataContext = createContext<AppData | null>(null)

export const AppDataProvider = AppDataContext.Provider

export function useAppData(): AppData {
  const value = useContext(AppDataContext)
  if (!value) throw new Error('useAppData must be used inside the dashboard frame.')
  return value
}
