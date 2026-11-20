// Request words (02-words WORDS-7, Appendix E): requests sent to a device,
// their steps and results, and the machine password (COMM Key) card.

import { plainStatus } from './states'
import type { PlainStatus } from './types'

/** E1 label of a request type; unknown types read "Request". */
export function requestTitle(commandType: string | null | undefined): string {
  const word = plainStatus('requestType', commandType)
  return word.unknown ? 'Request' : word.label
}

/** E2 status of a request. */
export const requestStatus = (status: string | null | undefined): PlainStatus => plainStatus('request', status)

/** The three steps a request goes through. */
export const REQUEST_STEPS = ['Waiting to send', 'Sent to the link box', 'Done'] as const

/** Which step (1-3) a request status is at. */
export function requestStep(status: string | null | undefined): 1 | 2 | 3 {
  const rank = requestStatus(status).rank
  return rank === 1 || rank === 2 || rank === 3 ? rank : 1
}

/** The end-of-step label: "Done", "Not done" or "Timed out" replace "Done" when the request ends badly. */
export function requestEndLabel(status: string | null | undefined): string {
  const upper = (status ?? '').toUpperCase()
  if (upper === 'FAILED') return 'Not done'
  if (upper === 'EXPIRED') return 'Timed out'
  if (upper === 'CANCELLED' || upper === 'CANCELED') return 'Cancelled'
  return 'Done'
}

export const REQUEST_KEPT = "We'll keep this request until it is done."
export const CANCEL_REQUEST = 'Cancel request'
export const REFRESH_PEOPLE_TITLE = 'Refreshing the people list from the machine'

// E5. Why a request failed (command.error_code from the link box).
export const REQUEST_FAILURES: Record<string, string> = {
  COMMAND_ALREADY_RUNNING: 'Another request is already running on this device.',
  COMMAND_CANCELLED: 'The request was cancelled.',
  COMMAND_EXECUTOR_BUSY: 'The link box is busy; try again in a minute.',
  COMMAND_EXPIRED: 'The request ran out of time.',
  COMMAND_RECEIPT_PERSIST_FAILED: "The link box did the work but couldn't save its note; IT should check it.",
  COMMAND_RECEIPT_READ_FAILED: "The link box did the work but couldn't save its note; IT should check it.",
  COMMAND_UNSUPPORTED: "This link box software can't do that request.",
  ESP_REBOOT_NOT_SAFE: "The link box can't restart safely right now; try again later.",
  ESP_REBOOT_SCOPE_OR_DEADLINE: 'The restart request was for another device or arrived too late.',
  ESP_REBOOT_UNSUPPORTED: "This link box software can't restart from the dashboard.",
  HIK_COMMAND_UNAVAILABLE: "This machine can't do that from the dashboard yet.",
  HIK_PROFILE_PRECONDITION_FAILED: "The person's details on the machine changed first, so nothing was changed. Refresh and try again.",
  HIK_PROFILE_REFRESH_PENDING: 'The list of people is still being read from the machine. Try again in a minute.',
  HIK_PROFILE_VERIFICATION_PENDING: 'The machine is still confirming the last change. Try again in a minute.',
  LEASE_EVIDENCE_REQUIRED: 'Changes to people are paused on this machine; tell IT.',
  LEASE_CHECKPOINT_FAILED: "The link box couldn't save the temporary admin access record. Try again.",
  ACTIVE_ADMIN_LEASE: 'Someone has temporary admin access on this machine. End it first, then try again.',
  ADMIN_LEASE_IDENTITY_REQUIRED: 'The machine must confirm this person before giving admin access.',
  ADMIN_LEASE_REVOCATION_REQUIRED: 'Temporary admin access must be removed first.',
  ADD_SNAPSHOT_SEND_FAILED: "The link box couldn't send the list of people to the dashboard. It will try again.",
  IDENTITY_TOMBSTONE_PERSIST_FAILED: "The link box couldn't save the removal note. Try again.",
  IDENTITY_TOMBSTONE_REQUIRED: 'The link box needs the newest list of people first. Try again in a minute.',
  LEGACY_USER_RECORD_READ_ONLY: "This person's record on the machine is in an old format and can't be changed here.",
  REBOOT_INBOX_NOT_DURABLE: "The link box couldn't save the restart request.",
  STORAGE_RECOVERY_ACTIVE: 'The link box is rescuing saved check-ins; try again after it finishes.',
  STORAGE_UPGRADE_NOT_CHECKED: "The link box memory hasn't been checked since the last update. Try again later.",
  TRUSTED_TIME_UNAVAILABLE: "The link box doesn't have the correct time yet. Try again in a minute.",
  USER_IDENTIFIER_CONFLICT: 'Another person on the machine already uses this Employee ID.',
  USER_NOT_FOUND: 'This person is no longer on the machine.',
  USER_PRECONDITION_FAILED: "The person's details on the machine changed first, so nothing was changed. Refresh and try again.",
  USER_SNAPSHOT_TRUNCATED: "The machine's list of people couldn't be read in full, so changes are paused.",
  ZKT_RESTART_FAILED: "The machine didn't restart. Try again.",
  ZKT_SERIAL_PERSIST_FAILED: "The link box couldn't save which machine it belongs to. Try again.",
  ZKT_SERIAL_PRECONDITION_FAILED: "The machine's serial number didn't match, so nothing was changed.",
  ZKT_USER_CREATE_FAILED: "The machine didn't add the person. Try again.",
  ZKT_USER_DELETE_VERIFY_FAILED: "The machine didn't confirm the person was removed. Refresh the people list to check.",
  ZKT_USER_POSTCONDITION_FAILED: "The change was made, but the check afterwards didn't pass. Refresh the people list to check.",
  ZKT_USER_READ_FAILED: "Couldn't read the person from the machine. Try again.",
  ZKT_USER_REREAD_FAILED: "Couldn't read the person again after the change. Refresh the people list to check.",
  ZKT_USER_WRITE_FAILED: "The machine didn't save the change. Try again.",
}
export const REQUEST_FAILURE_FALLBACK = "The request didn't work. Details are under Technical details."

// E4b. Why the last machine password change failed.
export const COMM_KEY_ERRORS: Record<string, string> = {
  COMM_KEY_CANDIDATE_AUTH_FAILED: "The machine didn't accept the new password. It can be tried again.",
  COMM_KEY_PERSIST_FAILED: "The link box couldn't save the new password. It can be tried again.",
  COMM_KEY_TERMINAL_SERIAL_MISMATCH: "The machine's serial number didn't match, so nothing was changed.",
  SERIAL_MISMATCH: "The machine's serial number didn't match, so nothing was changed.",
  COMM_KEY_SERIAL_PIN_FAILED: "The link box couldn't save which machine it belongs to.",
  COMM_KEY_TERMINAL_WRITE_UNSUPPORTED: "This link box can't change the password on the machine itself.",
  COMM_KEY_REVISION_CONFLICT: 'The password changed somewhere else; the dashboard needs to check which one is in use.',
  COMM_KEY_REVISION_DRIFT: 'The password changed somewhere else; the dashboard needs to check which one is in use.',
  COMM_KEY_ENVELOPE_INVALID: 'The change request arrived damaged, so nothing was changed.',
  COMM_KEY_OPERATION_EXPIRED: 'The change took too long and was dropped.',
  COMM_KEY_OPERATION_MISSING: 'The dashboard lost track of this change; nothing was changed.',
  COMM_KEY_STATE_MISSING: 'The dashboard lost track of this change; nothing was changed.',
  COMM_KEY_POSTCONDITION_FAILED: "The change was reported, but the check afterwards didn't pass.",
  COMM_KEY_INDETERMINATE: "The dashboard can't tell whether the change worked.",
  COMM_KEY_DIAGNOSTIC_VERIFIED_NO_COMMIT: 'The test by IT worked; nothing was changed.',
  TCP_4370_UNREACHABLE: "The link box couldn't reach the machine on the network.",
  CONNECT_NO_RESPONSE: "The machine didn't answer.",
  AUTH_NO_RESPONSE: "The machine didn't answer.",
  CONNECT_REJECTED: 'The machine refused the password.',
  AUTH_REJECTED: 'The machine refused the password.',
  SERIAL_READ_FAILED: "The machine didn't give its serial number.",
  SERIAL_EMPTY: "The machine didn't give its serial number.",
  AUTH_NOT_REQUIRED: "The machine doesn't use a password at the moment.",
}
export const COMM_KEY_ERROR_FALLBACK = "The last change didn't work. Details are under Technical details."

// E4c. Why it can't be changed on the machine from here.
export const COMM_KEY_BLOCKS: Record<string, string> = {
  FIRMWARE_UPDATE_REQUIRED: 'The link box needs a software update first.',
  TERMINAL_SERIAL_REQUIRED: "Confirm the machine's serial number first.",
  CURRENT_KEY_AUTHENTICATION_REQUIRED: 'The link box must first connect with the current password.',
  TERMINAL_MODEL_NOT_CERTIFIED: "This machine model isn't approved for remote changes.",
  CAPABILITY_NOT_AVAILABLE: 'Not available for this device.',
}
export const COMM_KEY_BLOCK_FALLBACK = 'Not available for this device.'

/** The plain sentence of a request failure code (E5, with the COMM Key codes of E4b). */
export function requestFailure(code: string | null | undefined): { sentence: string; unknown: boolean } {
  const key = (code ?? '').toUpperCase()
  const sentence = REQUEST_FAILURES[key] ?? COMM_KEY_ERRORS[key]
  return sentence ? { sentence, unknown: false } : { sentence: REQUEST_FAILURE_FALLBACK, unknown: true }
}

export function commKeyError(code: string | null | undefined): { sentence: string; unknown: boolean } {
  const sentence = COMM_KEY_ERRORS[(code ?? '').toUpperCase()]
  return sentence ? { sentence, unknown: false } : { sentence: COMM_KEY_ERROR_FALLBACK, unknown: true }
}

export const commKeyBlock = (code: string | null | undefined) => COMM_KEY_BLOCKS[(code ?? '').toUpperCase()] ?? COMM_KEY_BLOCK_FALLBACK

/** COMM Key card words (Words table, DeviceDrawer :417-434). */
export const COMM_KEY_CARD = {
  title: 'Machine password (COMM Key)',
  statusLine: (label: string) => `Password status: ${label}.`,
  lastFailed: (sentence: string) => `Last try failed: ${sentence}`,
  pendingSoftware: 'It will be applied after the link box software is updated.',
  switchedOff: 'Changing the machine password is switched off.',
  inProgress: (ends: string) => `Change in progress · ends ${ends}`,
  cancel: 'Cancel',
  modeLabel: 'Change it on',
  cantOnMachine: (sentence: string) => `Can't change it on the machine from here: ${sentence}`,
} as const

/** People change results (UsersWorkspace toasts). */
export const PEOPLE_RESULTS = {
  saved: (name: string) => `Saved on the machine: ${name} was changed.`,
  savedType: (commandType: string) => `Saved on the machine: ${requestTitle(commandType)}.`,
  notDone: (reason: string) => `Not done: ${reason} Try again.`,
  notDoneType: (commandType: string, status: string) => `Not done: ${requestTitle(commandType)}. ${requestStatus(status).explain}`,
  timedOut: 'Timed out: the change was not made.',
  stillRunning: 'A change for this person is still being made. Wait for it to finish.',
  adminAccess: (label: string) => `Temporary admin access: ${label}`,
} as const
