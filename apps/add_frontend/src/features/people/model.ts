// People page data shapes and rules (07-people). The extra fields come from
// the read-side API (PPL-2); they are optional so an older backend still works.
import type { Command, Device, DeviceUser, IdentityIntegrity, Lease } from '../../types'
import { friendlyNames } from '../../lib/deviceNames'
import { deviceDivision, type DivisionKey } from '../../lib/division'

export type PeopleUser = DeviceUser & {
  created_at?: string | null
  added_recently?: boolean
  current_command_id?: string | null
  looks_damaged?: boolean
}

export type PeopleIntegrity = IdentityIntegrity & {
  admins?: number
  added_recently?: number
  shift_workers?: number
}

export type PeopleLease = Lease & { user_id?: string | null; user_display_name?: string | null }

export type PeopleDevice = Omit<Device, 'active_lease'> & {
  active_lease?: PeopleLease | null
  active_command?: Command | null
}

export interface PeopleListResponse {
  rows: PeopleUser[]
  next_cursor: number | null
  next_offset?: number | null
  total?: number
  filtered_total?: number
  device?: Device
  identity_integrity?: PeopleIntegrity
}

export interface PeopleSummaryRow {
  connector_id: string
  people: number
  with_cnic: number
  needs_fixing: number
  no_cnic: number
  same_cnic_people: number
  cnic_typed_wrongly: number
  admins: number
  list_read_at: string | null
  admin_access: { user_id: string | null; user_display_name: string | null; expires_at: string | null } | null
}

export interface PeopleSummary {
  rows: PeopleSummaryRow[]
  computed_at?: string
}

export const ADMIN_PRIVILEGE = 14

/** Admins, view-only rows, rows with a change being sent and damaged entries can't be ticked. */
export const isBulkSelectionEligible = (user: PeopleUser) =>
  user.privilege !== ADMIN_PRIVILEGE && !user.read_only && !user.current_command_state && !user.looks_damaged

export const needsFixing = (user: PeopleUser) =>
  !user.cnic_available || Boolean(user.identity_conflict_code && !user.identity_conflict_resolved)

/** Friendly names for every device; the backend's friendly_name wins. */
export function deviceNameMap(devices: Pick<Device, 'connector_id' | 'display_name' | 'friendly_name'>[]): Map<string, string> {
  const fallback = new Map(friendlyNames(Array.from(new Set(devices.map((device) => device.display_name || '')))).map((named) => [named.code, named.name]))
  return new Map(devices.map((device) => [device.connector_id, device.friendly_name?.trim() || fallback.get(device.display_name || '') || device.display_name]))
}

export function friendlyName(device: Pick<Device, 'display_name' | 'friendly_name'> | null | undefined): string {
  if (!device) return ''
  if (device.friendly_name?.trim()) return device.friendly_name.trim()
  return friendlyNames([device.display_name || ''])[0]?.name || device.display_name
}

export const divisionOf = (device: Pick<Device, 'division' | 'display_name' | 'zone_id'>): DivisionKey => deviceDivision(device)

/** 13 digits (dashes or spaces allowed) means a CNIC search. */
export const cnicDigits = (text: string) => {
  const trimmed = text.trim()
  if (!/^[\d\s-]+$/.test(trimmed)) return ''
  return trimmed.replace(/[\s-]/g, '')
}

export const isCnicSearch = (text: string) => cnicDigits(text).length === 13

/** "35202-1234567-1" while typing; the API gets the 13 digits. */
export function formatCnicTyping(digits: string) {
  const value = digits.replace(/\D/g, '').slice(0, 13)
  if (value.length <= 5) return value
  if (value.length <= 12) return `${value.slice(0, 5)}-${value.slice(5)}`
  return `${value.slice(0, 5)}-${value.slice(5, 12)}-${value.slice(12)}`
}

/** Can people be added or changed on this machine at all (today's rule, unchanged). */
export function baseWritable(device: PeopleDevice | Device | null | undefined) {
  const zkt = device?.zkt
  return Boolean(zkt?.snapshot_complete && (zkt.certification_state === 'CERTIFIED'
    || (device?.firmware_family === 'hikvision' && ['PROFILE_PILOT', 'PROFILE_EXCEPTION'].includes(zkt.certification_state))))
}

export function machineBinding(device: PeopleDevice | Device | null | undefined) {
  const zkt = device?.zkt
  const migratedWithoutPin = Boolean(zkt?.serial && zkt.terminal_binding_state === 'CONFIRMED' && !zkt.expected_serial
    && zkt.confirmed_serial === zkt.serial && zkt.serial_confirmed_by === 'MIGRATED_PREEXISTING')
  const needsAction = Boolean(zkt?.serial && (zkt.terminal_binding_state === 'SERIAL_CONFIRMATION_REQUIRED'
    || ['TERMINAL_SERIAL_CONFIRMATION_REQUIRED', 'TERMINAL_SERIAL_PIN_FAILED'].includes(zkt.writes_disabled_reason || '')
    || migratedWithoutPin))
  const pending = Boolean(zkt?.terminal_binding_state === 'PENDING_DEVICE_ACK' || zkt?.writes_disabled_reason === 'TERMINAL_SERIAL_PENDING_DEVICE_ACK')
  return { migratedWithoutPin, needsAction, pending }
}

/** Merge a list response's device into the detail: never drop a running change or access (B9). */
export function mergeDevice(current: PeopleDevice | null, next: Device | PeopleDevice | null | undefined): PeopleDevice | null {
  if (!next) return current
  if (!current || current.connector_id !== next.connector_id) return next as PeopleDevice
  const incoming = next as PeopleDevice
  return {
    ...current,
    ...incoming,
    zkt: incoming.zkt ? { ...(current.zkt || {}), ...incoming.zkt } as PeopleDevice['zkt'] : current.zkt,
    active_command: 'active_command' in incoming ? incoming.active_command ?? null : current.active_command ?? null,
    active_lease: 'active_lease' in incoming ? incoming.active_lease ?? null : current.active_lease ?? null,
  }
}
