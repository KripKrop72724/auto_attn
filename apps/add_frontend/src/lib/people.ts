// Rules for adding, editing and removing people on a machine (moved from App.tsx).
import type { DeviceUser } from '../types'

export const utf8Length = (value: string) => new TextEncoder().encode(value).length

const utf8Prefix = (value: string, maxBytes: number) => {
  let output = ''
  for (const character of value) {
    if (utf8Length(output + character) > maxBytes) break
    output += character
  }
  return output
}

/** Old engineer wording; kept for older callers. People shows personProblems() from words/people. */
export function identityConflictText(user: DeviceUser) {
  const matches = user.identity_conflict_members
    .map((member) => `${member.user_id} (UID ${member.uid})`)
    .join(', ')
  if (user.identity_conflict_resolved) {
    return matches
      ? `${user.cnic_masked || 'Masked CNIC'} · Verified same employee across user ${matches}`
      : `${user.cnic_masked || 'Masked CNIC'} · Verified same-employee terminal alias`
  }
  return matches
    ? `${user.cnic_masked || 'Masked CNIC'} · Exact CNIC also encoded on user ${matches} · correction required`
    : `${user.cnic_masked || 'Masked CNIC'} · Exact duplicate reported by terminal · correction required`
}

export function buildMachinePreview(
  displayName: string,
  cnic: string,
  shiftWorker: boolean,
  byteLimit = 24,
) {
  const canonical = displayName.trim().replace(/\s+/g, ' ')
  const suffix = `-${shiftWorker ? 'S-' : ''}${cnic}`
  return `${utf8Prefix(canonical, Math.max(0, byteLimit - utf8Length(suffix)))}${suffix}`
}

export function validateUserDraft(values: {
  displayName: string
  cnic: string
  password: string
  userIdOverride?: string
}) {
  if (!values.displayName.trim()) return 'Type the full name.'
  if (!/^\d{13}$/.test(values.cnic)) return `CNIC must be 13 digits (${values.cnic.replace(/\D/g, '').length} typed).`
  if (values.userIdOverride && !/^\d+$/.test(values.userIdOverride)) {
    return 'Employee ID can only have digits.'
  }
  if (!values.password) return 'Type your password.'
  return null
}

export const confirmationMatches = (value: string, user: DeviceUser) =>
  value === user.display_name || value === user.user_id

export const bulkDeletionConfirmation = (count: number, deviceId: string) =>
  `DELETE ${count} USERS FROM ${deviceId}`
