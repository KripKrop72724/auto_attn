// Shared page types and constants (moved from App.tsx).
import type { DeviceUser, HistoricalIdentityCandidate, IdentityConflictGroup, ReconciliationJob } from '../types'

export type DrawerTab = 'overview' | 'logs' | 'alerts' | 'control'
export type UserDialogState =
  | { mode: 'create' }
  | { mode: 'edit'; user: DeviceUser }
  | { mode: 'delete'; user: DeviceUser }
  | { mode: 'lease'; user: DeviceUser }
  | null
export type IdentityResolutionDialogState = {
  mode: 'resolve' | 'revoke'
  group: IdentityConflictGroup
} | null
export type HistoricalIdentityDialogState = {
  candidate: HistoricalIdentityCandidate
} | null
export type ReconciliationDialogState =
  | { mode: 'start' }
  | { mode: 'control'; job: ReconciliationJob; action: 'pause' | 'resume' | 'cancel' | 'retry' }
  | null

export const terminalCommandStates = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'])
export const drawerTabs: DrawerTab[] = ['overview', 'logs', 'alerts', 'control']
