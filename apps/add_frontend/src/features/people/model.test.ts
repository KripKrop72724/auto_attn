import { describe, expect, it } from 'vitest'
import type { Device } from '../../types'
import { cnicDigits, formatCnicTyping, isBulkSelectionEligible, isCnicSearch, mergeDevice, type PeopleDevice, type PeopleUser } from './model'

const device = { connector_id: 'one', display_name: 'X', zkt: { serial: 'S', capabilities: { write_exception_permanent: true } } } as unknown as PeopleDevice

describe('People rules', () => {
  it('never lets a list answer drop a running change or access (B9)', () => {
    const detail = { ...device, active_command: { command_id: 'c', type: 'UPDATE_USER', status: 'RUNNING', created_at: '', expires_at: null }, active_lease: { lease_id: 'l', state: 'ACTIVE' } } as PeopleDevice
    const listDevice = { connector_id: 'one', display_name: 'X', state: 'ONLINE', zkt: { serial: 'S', capabilities: {} } } as unknown as Device
    const merged = mergeDevice(detail, listDevice)!
    expect(merged.active_command?.command_id).toBe('c')
    expect(merged.active_lease?.lease_id).toBe('l')
    expect(mergeDevice(detail, { ...listDevice, active_command: null } as Device)?.active_command).toBeNull()
  })

  it('treats 13 digits, with or without dashes, as a CNIC search', () => {
    expect(isCnicSearch('35202-1234567-1')).toBe(true)
    expect(isCnicSearch('3520212345671')).toBe(true)
    expect(isCnicSearch('1007')).toBe(false)
    expect(isCnicSearch('Ayesha 1234567890123')).toBe(false)
    expect(cnicDigits('35202 1234567 1')).toBe('3520212345671')
    expect(formatCnicTyping('3520212345671')).toBe('35202-1234567-1')
    expect(formatCnicTyping('35202')).toBe('35202')
  })

  it('keeps admins, view-only rows, busy rows and damaged entries out of a removal', () => {
    const base = { privilege: 0, read_only: false, current_command_state: null } as unknown as PeopleUser
    expect(isBulkSelectionEligible(base)).toBe(true)
    expect(isBulkSelectionEligible({ ...base, privilege: 14 })).toBe(false)
    expect(isBulkSelectionEligible({ ...base, read_only: true })).toBe(false)
    expect(isBulkSelectionEligible({ ...base, current_command_state: 'PENDING' })).toBe(false)
    expect(isBulkSelectionEligible({ ...base, looks_damaged: true })).toBe(false)
  })
})
