// Software update words (02-words WORDS-9, Appendix F6): the codes the link
// box reports during an update (`deployment.error_code`, `ota.last_error`,
// `FirmwareEvent.details.error_code`). Shared with 08-advanced ADV-2/ADV-11,
// which adds the page words and `softwareStatus()` to this file.
//
// The stored codes never change: test-device and factory holds key on
// `last_error_code`. Only what is shown changes.

import { plainStatus } from './states'
import type { PlainStatus } from './types'

export type UpdateCodeTone = 'wait' | 'hold' | 'failure'

export interface UpdateCodeWords {
  sentence: string
  tone: UpdateCodeTone
  /** main = under the device row; technical = only under Technical details. */
  where: 'main' | 'technical'
  next?: string
}

const c = (tone: UpdateCodeTone, where: 'main' | 'technical', sentence: string, next?: string): UpdateCodeWords => ({
  tone, where, sentence, ...(next ? { next } : {}),
})

const TELL_IT = 'Tell IT.'
const RETRY_THEN_IT = 'Press Try again; if it repeats, tell IT.'
const CHECK_INTERNET = 'Check the internet at this place, then press Try again.'

const BOOT_LOCAL_STEPS: Record<string, string> = {
  BOOT_LOCAL_STORAGE_UPGRADE: 'preparing its memory',
  BOOT_LOCAL_JOURNAL_RECOVERY: 'recovering saved check-ins',
  BOOT_LOCAL_STORAGE_UNVERIFIED: 'checking its memory',
  BOOT_LOCAL_TELEMETRY_LOCK: 'reading its own status',
  BOOT_LOCAL_TERMINAL_SESSION: 'reconnecting to the machine',
  BOOT_LOCAL_TERMINAL_COUNTS: 'counting records on the machine',
  BOOT_LOCAL_LEGACY_WORKERS: 'starting its senders',
}

/** F6, one entry per code (families below). */
export const UPDATE_CODES: Record<string, UpdateCodeWords> = {
  WAITING_FOR_RUNTIME_HEALTH: c('wait', 'main', 'Restarted on the new software; checking everything works before confirming.'),
  LOCAL_RUNTIME_HEALTHY: c('wait', 'main', 'The new software passed its first checks; finishing up.'),
  RUNTIME_HEALTHY: c('wait', 'main', 'The new software passed its first checks; finishing up.'),
  WAITING_FOR_ZKT_SAFEPOINT: c('wait', 'main', 'Waiting for a safe moment with the machine before restarting.'),
  WAITING_FOR_HIK_SAFEPOINT: c('wait', 'main', 'Waiting for a safe moment with the machine before restarting.'),
  WAITING_FOR_JOURNAL_QUIESCE: c('wait', 'main', 'Waiting for saved check-ins to settle before restarting.'),
  ...Object.fromEntries(Object.entries(BOOT_LOCAL_STEPS).map(([code, step]) => [code, c('wait', 'main', `Final checks after the restart: ${step} (up to 15 min).`)])),
  BOOT_LOCAL_UNSUPPORTED_FAMILY: c('failure', 'main', "This software can't run its start-up checks on this link box.", TELL_IT),
  BOOT_LOCAL_IMAGE_UNSUPPORTED: c('failure', 'main', "This software can't run its start-up checks on this link box.", TELL_IT),
  BOOT_LOCAL_MARK_VALID_FAILED: c('failure', 'main', "The new software started but couldn't be marked as good, so the link box will go back to the old version.", TELL_IT),
  STORAGE_RECOVERY_RUNNING: c('wait', 'main', 'Rescuing saved check-ins from the link box memory before confirming.'),
  STORAGE_REPAIR_EXCEPTION: c('wait', 'main', 'Running the special memory repair.'),
  OTA_ASSIGNMENT_ALREADY_RUNNING: c('wait', 'technical', 'Another update for this link box is already running.'),
  JOURNAL_READER_SELECTION_PENDING: c('wait', 'technical', 'Preparing a safe way back to the old version.'),
  BOOT_ROLLBACK_SELECTION_PENDING: c('wait', 'technical', 'Preparing a safe way back to the old version.'),
  BOOT_ROLLBACK_SESSION_CLEANUP: c('wait', 'technical', 'Finishing work with the machine and saved check-ins before switching back.'),
  BOOT_ROLLBACK_STORAGE_DRAIN: c('wait', 'technical', 'Finishing work with the machine and saved check-ins before switching back.'),
  FACTORY_TRIAL_SESSION_CLEANUP: c('wait', 'technical', 'Finishing work with the machine and saved check-ins before switching back.'),
  FACTORY_TRIAL_STORAGE_DRAIN: c('wait', 'technical', 'Finishing work with the machine and saved check-ins before switching back.'),
  BOOT_ROLLBACK_PREDECESSOR_UNQUALIFIED: c('hold', 'main', "The old version can't read the saved check-ins, so going back is blocked to keep them safe.", 'Tell IT before trying again.'),
  BOOT_ROLLBACK_WRITER_IDENTITY: c('hold', 'technical', 'Going back to the old version is paused to keep saved check-ins safe.', 'Tell IT if it stays more than an hour.'),
  BOOT_ROLLBACK_SELECTION_HELD: c('hold', 'technical', 'Going back to the old version is paused to keep saved check-ins safe.', 'Tell IT if it stays more than an hour.'),
  BOOT_ROLLBACK_SELECTION_UNAVAILABLE: c('hold', 'technical', 'Going back to the old version is paused to keep saved check-ins safe.', 'Tell IT if it stays more than an hour.'),
  BOOT_ROLLBACK_READER_RECOVERY: c('hold', 'technical', 'Going back to the old version is paused to keep saved check-ins safe.', 'Tell IT if it stays more than an hour.'),
  STORAGE_REDO_FLOOR: c('hold', 'main', 'This link box keeps saved check-ins in a backup area; only software 2.7.6 or later can be installed.', 'Choose a newer version.'),
  STORAGE_RECOVERY_NOT_AUTHORIZED: c('hold', 'main', "The memory rescue wasn't approved for this link box.", TELL_IT),
  DOWNLOAD_INCOMPLETE: c('failure', 'main', 'The download stopped before it finished.', CHECK_INTERNET),
  DOWNLOAD_INTERRUPTED: c('wait', 'main', 'The download was interrupted; it continues from where it stopped.'),
  DOWNLOAD_BEGIN_FAILED: c('failure', 'main', "The link box couldn't start the download.", RETRY_THEN_IT),
  DOWNLOAD_OR_SIGNATURE_FAILED: c('failure', 'main', "The download didn't finish or failed its safety check.", CHECK_INTERNET),
  IMAGE_DESCRIPTOR_MISMATCH: c('failure', 'main', "The downloaded software isn't the expected file, so it wasn't installed.", RETRY_THEN_IT),
  IMAGE_LENGTH_MISMATCH: c('failure', 'main', "The downloaded software isn't the expected file, so it wasn't installed.", RETRY_THEN_IT),
  IMAGE_HASH_MISMATCH: c('failure', 'main', "The downloaded software isn't the expected file, so it wasn't installed.", RETRY_THEN_IT),
  PARTITION_HASH_FAILED: c('failure', 'main', "The downloaded software didn't check out on the link box.", 'Press Try again; it downloads again from the start.'),
  IMAGE_TOO_LARGE: c('failure', 'main', 'This software is too big for this link box.', TELL_IT),
  IMAGE_SIZE_ALIGNMENT: c('failure', 'technical', "The link box couldn't prepare space for the update.", TELL_IT),
  PARTITION_ERASE_ALIGNMENT: c('failure', 'technical', "The link box couldn't prepare space for the update.", TELL_IT),
  OTA_RESUME_OFFSET_INVALID: c('failure', 'technical', "The link box couldn't prepare space for the update.", TELL_IT),
  BOOT_HEALTH_TIMEOUT: c('failure', 'main', "The new software didn't pass its start-up checks in time, so the link box went back to the old version.", 'Check the machine is connected and the power is steady, then try again.'),
  BOOT_CONFIRM_CAP: c('failure', 'main', 'The new software restarted too many times before confirming.', "Check the link box's power, then try again."),
  PREVIOUS_FIRMWARE_OBSERVED: c('failure', 'main', 'The link box is back on its old software after the update.', 'Check the power at the place, then try again.'),
  BOOTLOADER_ROLLBACK: c('failure', 'main', 'The link box is back on its old software after the update.', 'Check the power at the place, then try again.'),
  RUNNING_VERSION_MISMATCH: c('failure', 'main', 'The link box is back on its old software after the update.', 'Check the power at the place, then try again.'),
  STORAGE_RECOVERY_TIMEOUT: c('failure', 'main', 'The memory rescue took too long; the link box went back to its old software.', TELL_IT),
  STORAGE_RECOVERY_ROLLBACK_FAILED: c('failure', 'main', "The memory rescue couldn't go back to the old software; the link box stays reachable for a reinstall.", TELL_IT),
  STORAGE_REPAIR_EXCEPTION_UNHEALTHY: c('failure', 'main', "The special memory repair didn't fix this link box.", 'Replace or re-install the link box (IT).'),
  STORAGE_REPAIR_EXCEPTION_UNREPAIRED: c('failure', 'main', "The special memory repair didn't fix this link box.", 'Replace or re-install the link box (IT).'),
  FAILED_BOOT_INTENT: c('wait', 'technical', 'Update step recorded.'),
  READER_INTENT: c('wait', 'technical', 'Update step recorded.'),
  BOOT_REPORTED: c('wait', 'technical', 'Update step recorded.'),
  LOCAL_VALIDATED: c('wait', 'technical', 'Update step recorded.'),
}

const JOURNAL_FAILURES = new Set(['OTA_JOURNAL_COMMIT_FAILED', 'OTA_JOURNAL_CORRUPT', 'OTA_JOURNAL_INVALID', 'OTA_JOURNAL_OPEN_FAILED', 'OTA_JOURNAL_READ_FAILED', 'OTA_LEGACY_JOURNAL_CORRUPT'])

/** Plain step names for memory rescue results (STORAGE_RECOVERY_*). */
const RESCUE_STEPS: Record<string, string> = {
  NOT_STARTED: 'it did not start',
  ADD_UNAVAILABLE: "the dashboard couldn't be reached",
  BUILD_MISMATCH: "the rescue software doesn't match",
  CURSOR_UNCHANGED: 'nothing new was rescued',
  IDENTITY: "the link box details didn't match",
  INVENTORY_BEFORE: 'checking the memory first',
  INVENTORY_AFTER: 'checking the memory afterwards',
  MOUNT: "the memory wouldn't open",
  NOT_PENDING_VERIFY: 'it was not waiting to be confirmed',
  ORPHAN_COMPLETE: 'every loose check-in was rescued',
  ORPHAN_SEEN: 'found loose check-ins',
  ORPHAN_SEEN_PARTIAL: 'found some loose check-ins',
  ROLLBACK_IMAGE: 'preparing the old software',
  ROLLBACK_STATE: 'going back to the old software',
  SLOT_LAYOUT: "the link box layout doesn't fit",
  STARTED: 'started',
  TARGET_MISMATCH: 'it was meant for another link box',
}

/** Rescue steps that are progress, not failures. */
const RESCUE_PROGRESS = new Set(['STARTED', 'ORPHAN_COMPLETE', 'ORPHAN_SEEN', 'ORPHAN_SEEN_PARTIAL', 'INVENTORY_BEFORE', 'INVENTORY_AFTER', 'ROLLBACK_IMAGE', 'ROLLBACK_STATE'])

const FAMILIES: Array<[RegExp, (code: string) => UpdateCodeWords]> = [
  [/^JOURNAL_/, () => c('hold', 'technical', 'The update is paused: the new software must first prove it can read the saved check-ins.', 'Tell IT if it stays more than an hour.')],
  [/^FACTORY_TRIAL_/, () => c('hold', 'technical', 'Factory test step recorded (test devices only).')],
  [/^STORAGE_RECOVERY_/, (code) => {
    const name = code.replace(/^STORAGE_RECOVERY_/, '')
    const step = RESCUE_STEPS[name]
    const sentence = step ? `Memory rescue result: ${step}.` : 'Memory rescue result recorded.'
    return RESCUE_PROGRESS.has(name) ? c('wait', 'technical', sentence) : c('failure', 'technical', sentence, TELL_IT)
  }],
]

export const UPDATE_CODE_FALLBACK = "The link box reported an update step the dashboard has no words for yet."

/**
 * Plain words for an update code. Unknown codes: a failure only when the
 * update itself is FAILED or ROLLED_BACK, otherwise a wait.
 */
export function plainUpdateCode(code: string | null | undefined, deploymentStatus?: string | null): UpdateCodeWords & { code: string; unknown: boolean } {
  const key = (code ?? '').trim().toUpperCase()
  const known = UPDATE_CODES[key]
    ?? (JOURNAL_FAILURES.has(key) ? c('failure', 'main', "The link box couldn't save its update notes.", 'Restart the link box, then press Try again. If it repeats, tell IT.') : undefined)
    ?? FAMILIES.find(([pattern]) => pattern.test(key))?.[1](key)
  if (known) return { ...known, code: code ?? '', unknown: false }
  const failed = ['FAILED', 'ROLLED_BACK'].includes((deploymentStatus ?? '').toUpperCase())
  return { ...c(failed ? 'failure' : 'wait', 'main', UPDATE_CODE_FALLBACK), code: code ?? '', unknown: true }
}

/** "Update step: {label}" (device update panel). */
export const updateStepLine = (otaState: string | null | undefined) => `Update step: ${plainStatus('otaState', otaState).label}`

/** "Last update message: {sentence}". */
export const lastUpdateMessage = (code: string | null | undefined) => `Last update message: ${plainUpdateCode(code).sentence}`

/** Wait reason of a rollout target; unknown reads "Waiting". */
export function updateWaitLabel(code: string | null | undefined): string {
  const word: PlainStatus = plainStatus('updateWait', code)
  return word.unknown ? 'Waiting' : word.label
}

/** Update drawer row labels (Firmware.tsx :584-590). */
export const UPDATE_ROW_LABELS = {
  hold: 'Paused',
  interruption: 'Last interruption',
  report: 'Latest step',
  failure: 'Why it failed',
} as const

export const UNDO_UNKNOWN = "The dashboard couldn't tell why the update was undone."
export const NO_TEST_VERSION = 'No version ready for test devices'
