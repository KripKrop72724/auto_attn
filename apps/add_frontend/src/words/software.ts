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

// ---------------------------------------------------------------------------
// Software updates page words and the one software-status rule (08-advanced
// ADV-2 / ADV-11, W2). `softwareStatus()` is the only place that decides a
// device's software words: the Software updates page, the Devices column and
// the device detail all call it, so they never contradict each other again.
// ---------------------------------------------------------------------------

/** The kit chip tone a word should use (ui/StatusChip `Tone`, kept here as a plain union). */
export type SoftwareChip = 'working' | 'attention' | 'offline' | 'neutral' | 'info'

/** Machine brand of a link box's software. */
export type SoftwareFamily = 'zkt' | 'hikvision'

export const FAMILY_NAMES: Record<SoftwareFamily, string> = { zkt: 'ZKTeco', hikvision: 'Hikvision' }
export const familyName = (family: string | null | undefined): string => FAMILY_NAMES[(family === 'hikvision' ? 'hikvision' : 'zkt')]

/** "2.7.8" from "zone-lite-2.7.8", "ZKT Zone Lite 2.7.8" or "2.7.8"; null when there is no version. */
export function plainVersion(value: string | null | undefined): string | null {
  if (!value) return null
  const matches = String(value).match(/\d+(?:\.\d+)+/g)
  return matches ? matches[matches.length - 1] : null
}

/** Compares two versions number by number ("2.7.10" > "2.7.9"); null when either is unknown. */
export function compareVersions(left: string | null | undefined, right: string | null | undefined): number | null {
  const a = plainVersion(left)
  const b = plainVersion(right)
  if (!a || !b) return null
  const x = a.split('.').map(Number)
  const y = b.split('.').map(Number)
  for (let index = 0; index < Math.max(x.length, y.length); index += 1) {
    const diff = (x[index] ?? 0) - (y[index] ?? 0)
    if (diff) return diff
  }
  return 0
}

/** W2.3: the step a device is on during an update. */
export const UPDATE_STEPS: Record<string, string> = {
  PENDING: 'Waiting its turn',
  OFFERED: 'Starting download',
  DOWNLOADING: 'Downloading',
  VERIFYING: 'Checking the download',
  READY_TO_BOOT: 'Ready to restart',
  BOOTED_PENDING: 'Restarting into the new version',
  RECONCILING: 'Final checks (can take up to 15 min)',
  SUCCEEDED: 'Updated',
  FAILED: 'Failed',
  ROLLED_BACK: 'Went back to the old version',
  CANCELLED: 'Stopped',
  SUPERSEDED: 'Replaced by a newer try',
  RELEASE_REVOKED: 'Version withdrawn',
}

/** "Downloading 45%" or the plain step; unknown steps read "Update step not known yet". */
export function updateStepWord(status: string | null | undefined, percent?: number | null): string {
  const key = (status ?? '').toUpperCase()
  const word = UPDATE_STEPS[key]
  if (!word) return 'Update step not known yet'
  return key === 'DOWNLOADING' && typeof percent === 'number' ? `${word} ${percent}%` : word
}

/** The step strip on an update card (W2.3, 2.3): five steps from waiting to updated. */
export const UPDATE_STRIP = ['Waiting', 'Downloading', 'Checking', 'Restarting', 'Final checks', 'Updated'] as const

/** Index of the strip step a deployment state has reached (0 = Waiting … 5 = Updated). */
export function updateStripIndex(status: string | null | undefined): number {
  switch ((status ?? '').toUpperCase()) {
    case 'OFFERED':
    case 'DOWNLOADING': return 1
    case 'VERIFYING':
    case 'READY_TO_BOOT': return 2
    case 'BOOTED_PENDING': return 3
    case 'RECONCILING': return 4
    case 'SUCCEEDED': return 5
    default: return 0
  }
}

/** W2.4: failure sentences shown on the card, the sheet and Overview. */
export const UPDATE_FAILURES: Record<string, string> = {
  TARGET_BOOT_INTERRUPTED: 'The link box restarted before it finished installing and went back to the old version. Attendance is safe.',
  DOWNLOAD_INTERRUPTED: 'The download stopped part-way, usually because the internet dropped. It carries on by itself.',
  PARTITION_HASH_FAILED: 'The downloaded file was damaged and was not installed. Try again.',
  PREVIOUS_FIRMWARE_OBSERVED: 'The link box went back to the old version after starting the new one. The cause is not known.',
  IMAGE_HASH_MISMATCH: "The downloaded file didn't match this version. It was not used.",
  IMAGE_DESCRIPTOR_MISMATCH: "The downloaded file didn't match this version. It was not used.",
  IMAGE_TOO_LARGE: 'This version is too big for this link box.',
  DOWNLOAD_BEGIN_FAILED: "The link box couldn't download the update safely.",
  DOWNLOAD_OR_SIGNATURE_FAILED: "The link box couldn't download the update safely.",
  STORAGE_REDO_FLOOR: 'This link box needs version 2.7.6 or newer first.',
  STORAGE_REPAIR_EXCEPTION: 'The link box refused to repair its storage. IT must check it.',
}

export const UPDATE_FAILURE_FALLBACK = 'The link box reported a problem with the update. IT can see the details below.'
export const FINAL_CHECKS_AFTER_RESTART = 'Final checks after restart.'

/** One plain sentence for a device's update problem code (never the code itself). */
export function updateFailureSentence(code: string | null | undefined, deploymentStatus?: string | null): string {
  const key = (code ?? '').trim().toUpperCase()
  if (!key) return ['ROLLED_BACK'].includes((deploymentStatus ?? '').toUpperCase())
    ? 'The link box went back to the old version. Attendance is safe.'
    : UPDATE_FAILURE_FALLBACK
  if (UPDATE_FAILURES[key]) return UPDATE_FAILURES[key]
  if ((/^WAITING_FOR_/.test(key) || /^BOOT_LOCAL_/.test(key)) && key !== 'BOOT_LOCAL_MARK_VALID_FAILED') return FINAL_CHECKS_AFTER_RESTART
  const known = plainUpdateCode(key, deploymentStatus)
  return !known.unknown && known.where === 'main' ? known.sentence : UPDATE_FAILURE_FALLBACK
}

// Deployment states while an update is moving on one device.
const MOVING = new Set(['OFFERED', 'DOWNLOADING', 'VERIFYING', 'READY_TO_BOOT', 'BOOTED_PENDING', 'RECONCILING'])
const FAILED_STATES = new Set(['FAILED', 'ROLLED_BACK'])

/** The part of a device row the rule reads. */
export interface SoftwareDevice {
  connector_id: string
  firmware_version: string | null
  firmware_family?: string | null
  ota_capable?: boolean
  ota_state?: string | null
  is_spare?: boolean
}

/** One device's part of a live (running or paused) update. */
export interface LiveDeployment {
  connector_id: string | null
  status: string
  target_version: string
  previous_version?: string | null
  bytes_written?: number
  error_code?: string | null
  /** Size of the version being downloaded, for the download %. */
  image_size?: number | null
  /** The update this belongs to (campaign id), to open it. */
  update_id?: string
  updated_at?: string
}

export interface SoftwareRelease {
  version: string
  state: string
  firmware_family?: string | null
}

export interface SoftwareContext {
  devicesLoaded: boolean
  /** The version catalog; null or undefined when not loaded (then no "Up to date" claim is made). */
  releases?: SoftwareRelease[] | null
  /** Deployments of running and paused updates (loaded once by the page). */
  liveDeployments?: LiveDeployment[] | null
  /** Test versions made for this exact device (connector id → versions). */
  testVersions?: Map<string, string[]> | null
}

export type SoftwareKey =
  | 'loading' | 'spare' | 'updating' | 'waiting' | 'failed' | 'blocked' | 'technician'
  | 'unconfirmed' | 'unknown' | 'current' | 'newer' | 'version'

export interface SoftwareStatus {
  key: SoftwareKey
  /** The chip / heading words. */
  label: string
  /** Short form for list columns ("2.7.6 · Up to date"). */
  short: string
  tone: 'good' | 'neutral' | 'warning' | 'problem'
  chip: SoftwareChip
  /** Plain reason (failures, waits). */
  sentence?: string
  /** Counted in totals (spares and loading rows are not). */
  counted: boolean
  /** The plain running version ("2.7.6"). */
  version: string | null
  /** The newest version for all devices of this brand, when known. */
  newest: string | null
  /** The live deployment this status comes from. */
  deployment?: LiveDeployment
  /** Download percentage while downloading. */
  percent?: number
}

const ACTIVE_ORDER: Record<string, number> = { DOWNLOADING: 0, VERIFYING: 0, READY_TO_BOOT: 0, BOOTED_PENDING: 0, RECONCILING: 0, OFFERED: 0, PENDING: 1, FAILED: 2, ROLLED_BACK: 2 }

/** The live deployment that matters for one device: moving first, then waiting, then failed. */
export function liveDeploymentFor(connectorId: string, deployments: LiveDeployment[] | null | undefined): LiveDeployment | undefined {
  return (deployments ?? [])
    .filter((row) => row.connector_id === connectorId && ACTIVE_ORDER[row.status] !== undefined)
    .sort((a, b) => (ACTIVE_ORDER[a.status] - ACTIVE_ORDER[b.status]) || String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? '')))[0]
}

/** Newest version for all devices of one brand. */
export function newestVersion(releases: SoftwareRelease[] | null | undefined, family: string | null | undefined): string | null {
  const wanted = family === 'hikvision' ? 'hikvision' : 'zkt'
  let best: string | null = null
  for (const release of releases ?? []) {
    if (release.state !== 'AVAILABLE') continue
    if ((release.firmware_family === 'hikvision' ? 'hikvision' : 'zkt') !== wanted) continue
    if (!best || (compareVersions(release.version, best) ?? 0) > 0) best = plainVersion(release.version)
  }
  return best
}

export const downloadPercent = (deployment: LiveDeployment): number | undefined =>
  deployment.status === 'DOWNLOADING' && deployment.image_size && deployment.bytes_written !== undefined
    ? Math.max(0, Math.min(99, Math.floor((deployment.bytes_written / deployment.image_size) * 100)))
    : undefined

/**
 * The software words for one device (rule table in 08-advanced 2.1), checked in
 * this order: loading, spare, updating, waiting, failed, can't update now,
 * technician, not confirmed, version unknown, up to date, newer available.
 */
export function softwareStatus(device: SoftwareDevice, context: SoftwareContext): SoftwareStatus {
  const version = plainVersion(device.firmware_version)
  const newest = newestVersion(context.releases, device.firmware_family)
  const make = (key: SoftwareKey, label: string, short: string, tone: SoftwareStatus['tone'], chip: SoftwareChip, extra: Partial<SoftwareStatus> = {}): SoftwareStatus =>
    ({ key, label, short, tone, chip, counted: true, version, newest, ...extra })
  if (!context.devicesLoaded) return make('loading', 'Loading…', 'Loading…', 'neutral', 'neutral', { counted: false })
  if (device.is_spare) return make('spare', 'Spare (not in use)', 'Spare', 'neutral', 'neutral', { counted: false })
  const live = liveDeploymentFor(device.connector_id, context.liveDeployments)
  const from = plainVersion(live?.previous_version) ?? version
  const to = plainVersion(live?.target_version)
  const arrow = from && to ? `${from} → ${to}` : to ?? from ?? ''
  if (live && MOVING.has(live.status)) {
    const percent = downloadPercent(live)
    return make('updating', `Updating · ${updateStepWord(live.status, percent)}`, `${arrow ? `${arrow} · ` : ''}Updating`, 'neutral', 'info', { deployment: live, percent })
  }
  if (live?.status === 'PENDING') return make('waiting', 'Waiting its turn', 'Update waiting', 'warning', 'attention', { deployment: live })
  if ((live && FAILED_STATES.has(live.status)) || device.ota_state === 'ROLLBACK_REQUIRED') {
    return make('failed', 'Update failed', 'Update failed', 'problem', 'offline', {
      deployment: live, sentence: updateFailureSentence(live?.error_code, live?.status ?? 'ROLLED_BACK'),
    })
  }
  if (device.ota_state === 'OTA_BLOCKED') {
    return make('blocked', "Can't update remotely right now", "Can't update now", 'warning', 'attention', { sentence: 'The link box refused the last update. Ask IT.' })
  }
  if (device.ota_capable === false || device.ota_state === 'LEGACY_MANUAL_UPDATE') {
    return make('technician', 'Needs a technician to update (cable)', 'Technician needed', 'neutral', 'neutral')
  }
  if (device.ota_state === 'UPDATING') return make('unconfirmed', 'Update status not confirmed yet', 'Not confirmed yet', 'warning', 'attention')
  if (!version) return make('unknown', 'Version not known yet', 'Version unknown', 'neutral', 'neutral')
  const tests = context.testVersions?.get(device.connector_id) ?? []
  if (tests.some((test) => compareVersions(test, version) === 0)) return make('current', 'Up to date', `${version} · Up to date`, 'good', 'working')
  if (!context.releases) return make('version', `Version ${version}`, version, 'neutral', 'neutral')
  if (!newest || (compareVersions(version, newest) ?? -1) >= 0) return make('current', 'Up to date', `${version} · Up to date`, 'good', 'working')
  return make('newer', 'Newer version available', `${version} · Newer available`, 'neutral', 'info')
}

export interface SoftwareCounts {
  total: number
  spares: number
  current: number
  updating: number
  waiting: number
  failed: number
  technician: number
  blocked: number
  newer: number
  other: number
}

/** Counts for the status line (spares are left out and counted apart). */
export function softwareCounts(devices: SoftwareDevice[], context: SoftwareContext): SoftwareCounts {
  const counts: SoftwareCounts = { total: 0, spares: 0, current: 0, updating: 0, waiting: 0, failed: 0, technician: 0, blocked: 0, newer: 0, other: 0 }
  for (const device of devices) {
    const status = softwareStatus(device, context)
    if (status.key === 'spare') { counts.spares += 1; continue }
    if (!status.counted) continue
    counts.total += 1
    if (status.key === 'current') counts.current += 1
    else if (status.key === 'updating') counts.updating += 1
    else if (status.key === 'waiting') counts.waiting += 1
    else if (status.key === 'failed') counts.failed += 1
    else if (status.key === 'technician') counts.technician += 1
    else if (status.key === 'blocked') counts.blocked += 1
    else if (status.key === 'newer') counts.newer += 1
    else counts.other += 1
  }
  return counts
}

/** Overview filter chips and the order a list sorts by (needs attention first). */
export const SOFTWARE_SHOW = {
  attention: 'Needs attention',
  updating: 'Updating',
  newer: 'Newer version available',
  current: 'Up to date',
  technician: 'Technician needed',
} as const
export type SoftwareShow = keyof typeof SOFTWARE_SHOW

export const softwareShowFor = (key: SoftwareKey): SoftwareShow | null =>
  key === 'failed' || key === 'blocked' || key === 'unconfirmed' ? 'attention'
    : key === 'updating' || key === 'waiting' ? 'updating'
      : key === 'newer' || key === 'unknown' || key === 'version' ? 'newer'
        : key === 'current' ? 'current'
          : key === 'technician' ? 'technician' : null

export const SOFTWARE_RANK: Record<SoftwareKey, number> = {
  failed: 0, blocked: 1, unconfirmed: 2, updating: 3, waiting: 4, newer: 5, unknown: 6, version: 7, technician: 8, current: 9, spare: 10, loading: 11,
}

// --- Update (campaign) chips, pause reasons and page words (W2.1-W2.6) ---

export interface UpdateRowLike {
  status: string
  archived_at?: string | null
  waiting?: { code: string } | null
  pause_reason?: string | null
  pause_kind?: string | null
  pause_code?: string | null
  pause_device_name?: string | null
  counts?: Record<string, number>
  actor?: string | null
}

export type UpdateShow = 'attention' | 'updating' | 'waiting' | 'paused' | 'finished' | 'stopped'

export const UPDATE_SHOW: Record<UpdateShow, string> = {
  attention: 'Waiting for you',
  updating: 'Updating',
  waiting: 'Waiting for device',
  paused: 'Paused',
  finished: 'Finished',
  stopped: 'Stopped',
}

const DEVICE_OUTCOME = /^(.+?): (FAILED|ROLLED_BACK) \(([^()]*)\)$/

export interface PauseWords {
  kind: 'DEVICE_FAILED' | 'DEVICE_ROLLED_BACK' | 'ONE_CLICK' | 'RELEASE_REVOKED' | 'OTHER' | null
  code: string | null
  /** The paused device's connector id (from the stored text) when the row has no name. */
  connectorId: string | null
  deviceName: string | null
  /** The user's withdraw note, for "Version withdrawn: {note}". */
  note: string | null
}

/** Reads the stored pause reason: the row's own fields first (ADV-17), else the stored text. */
export function readPause(row: UpdateRowLike): PauseWords {
  const reason = row.pause_reason ?? ''
  const note = reason.startsWith('Release revoked:') ? reason.slice('Release revoked:'.length).trim() || null : null
  if (row.pause_kind) {
    const match = DEVICE_OUTCOME.exec(reason)
    return { kind: row.pause_kind as PauseWords['kind'], code: row.pause_code ?? null, connectorId: match?.[1] ?? null, deviceName: row.pause_device_name ?? null, note }
  }
  if (!reason) return { kind: null, code: null, connectorId: null, deviceName: null, note: null }
  const match = DEVICE_OUTCOME.exec(reason)
  if (match) return { kind: match[2] === 'ROLLED_BACK' ? 'DEVICE_ROLLED_BACK' : 'DEVICE_FAILED', code: match[3] || null, connectorId: match[1], deviceName: null, note: null }
  if (reason === 'Paused with one click') return { kind: 'ONE_CLICK', code: null, connectorId: null, deviceName: null, note: null }
  if (note !== null || reason.startsWith('Release revoked:')) return { kind: 'RELEASE_REVOKED', code: null, connectorId: null, deviceName: null, note }
  return { kind: 'OTHER', code: null, connectorId: null, deviceName: null, note: null }
}

export const isDevicePause = (pause: PauseWords) => pause.kind === 'DEVICE_FAILED' || pause.kind === 'DEVICE_ROLLED_BACK'

/** The sentence under a paused update ("Paused because … could not update: …"). */
export function pauseSentence(pause: PauseWords, names: { device?: string | null; actor?: string | null }): string | null {
  if (isDevicePause(pause)) {
    return `Paused because ${names.device || 'a device'} could not update: ${updateFailureSentence(pause.code, pause.kind === 'DEVICE_ROLLED_BACK' ? 'ROLLED_BACK' : 'FAILED')}`
  }
  if (pause.kind === 'ONE_CLICK') return `Paused by ${names.actor || 'someone'}.`
  if (pause.kind === 'RELEASE_REVOKED') return 'Version withdrawn.'
  if (pause.kind === 'OTHER') return 'Paused.'
  return null
}

/** The filter group an update belongs to. */
export function updateShowFor(row: UpdateRowLike): UpdateShow {
  if (row.status === 'ACTIVE') return row.waiting ? 'waiting' : 'updating'
  if (row.status === 'PAUSED') return isDevicePause(readPause(row)) || failedCount(row) > 0 ? 'attention' : 'paused'
  if (row.status === 'COMPLETED') return 'finished'
  return 'stopped'
}

const failedCount = (row: UpdateRowLike) => (row.counts?.FAILED ?? row.counts?.failed ?? 0) + (row.counts?.ROLLED_BACK ?? row.counts?.rolled_back ?? 0)

/** True for an update that needs a person (the server's `attention` rule). */
export const updateNeedsYou = (row: UpdateRowLike) =>
  (row.status === 'PAUSED' && isDevicePause(readPause(row))) || (['ACTIVE', 'PAUSED'].includes(row.status) && failedCount(row) > 0)

/** The chip on an update card. */
export function updateChip(row: UpdateRowLike): { label: string; chip: SoftwareChip; show: UpdateShow | 'hidden' } {
  if (row.archived_at) return { label: 'Hidden', chip: 'neutral', show: 'hidden' }
  if (updateNeedsYou(row)) return { label: UPDATE_SHOW.attention, chip: 'offline', show: 'attention' }
  const show = updateShowFor(row)
  const chip: Record<UpdateShow, SoftwareChip> = { attention: 'offline', updating: 'info', waiting: 'attention', paused: 'attention', finished: 'working', stopped: 'neutral' }
  return { label: UPDATE_SHOW[show], chip: chip[show], show }
}

/** W2.5: retry refusals from the server, one plain sentence each (matched by text). */
export const RETRY_REFUSALS: Array<{ match: RegExp; sentence: string }> = [
  { match: /^The campaign must be paused/, sentence: 'Pause the update first.' },
  { match: /^A cancelled campaign cannot retry/, sentence: 'This update was stopped; start a new one.' },
  { match: /^The signed release is no longer available/, sentence: 'This version was withdrawn.' },
  { match: /own return evidence and HIL schedule/, sentence: 'This is a test version with its own plan; start a new update.' },
  { match: /can lose queued punches/, sentence: 'Going back to the old version can lose waiting check-ins. Recover attendance from the machine first, then start a new update.' },
  { match: /^A later attempt for this ESP already exists/, sentence: 'This device already has a newer try.' },
  { match: /^Another deployment in this campaign is still active/, sentence: 'Another device here is still updating; wait for it.' },
  { match: /did not verify on this ESP/, sentence: 'Failed twice; a technician must check the power first.' },
  { match: /were interrupted\. Capture the/, sentence: 'Interrupted twice; a technician must visit first.' },
  { match: /^The campaign was paused for another reason/, sentence: 'Paused for another reason; continue it or start a new update.' },
  { match: /^The ESP is no longer active in this campaign/, sentence: 'This device was moved or replaced.' },
  { match: /^The ESP is offline/, sentence: 'This device is offline; try when it is back.' },
  { match: /^The ESP no longer runs /, sentence: 'This device is no longer on the version it had before the update.' },
  { match: /^The ESP is not running from an OTA application slot/, sentence: "This device can't be updated remotely right now." },
  { match: /storage I\/O faults/, sentence: 'This device has a storage problem; fix that first.' },
  { match: /stability warnings (need|changed)/, sentence: 'Something changed since you opened this. Close and open it again.' },
]
export const RETRY_REFUSAL_FALLBACK = "This device can't try again right now."

/** "Can't try again yet: …" sentence for a server refusal. */
export function retryRefusalSentence(message: string | null | undefined): string {
  const text = (message ?? '').trim()
  return RETRY_REFUSALS.find(({ match }) => match.test(text))?.sentence ?? RETRY_REFUSAL_FALLBACK
}

/** Server text for update actions (3f), matched by text; unknown text gets the fallback. */
export const UPDATE_ACTION_ERRORS: Array<{ match: RegExp; sentence: string }> = [
  { match: /already runs campaign/, sentence: 'This device already has an update running.' },
  { match: /No device in this zone is eligible/, sentence: "This device can't take this version." },
  { match: /started in zone .* meanwhile/, sentence: 'Someone started an update for this device at the same moment.' },
  { match: /^Already created by this request/, sentence: 'Already started.' },
  { match: /idempotency key belongs/, sentence: 'Something went wrong. Close and try again.' },
  { match: /Choose 1 to 30 distinct zones/, sentence: 'Choose up to 30 devices at a time.' },
  { match: /OTA remains disabled|quarantine is disabled/, sentence: 'Updates are switched off on the server.' },
  { match: /HIL release target does not match|HIL campaign requires/, sentence: 'This device is not a test device for this version.' },
  { match: /^Firmware release is not available|lacks the ESP application digest/, sentence: "This version can't be installed right now." },
  { match: /campaign cannot be (paused|resumed|cancelled|archived)/, sentence: 'This update has already changed. Refresh the list.' },
  { match: /cannot resume because its signed release is unavailable/, sentence: 'This version was withdrawn.' },
  { match: /already has another active or paused firmware campaign/, sentence: 'Another update for this device is running or paused.' },
  { match: /database is busy/, sentence: 'The dashboard is busy. Press Clear history again.' },
  { match: /Password confirmation failed|Invalid password|password/i, sentence: 'Wrong password. Try again.' },
]
export const UPDATE_ACTION_FALLBACK = "Couldn't change the update. Try again."

export function updateActionError(message: string | null | undefined, fallback = UPDATE_ACTION_FALLBACK): string {
  const text = (message ?? '').trim()
  return UPDATE_ACTION_ERRORS.find(({ match }) => match.test(text))?.sentence ?? fallback
}

/** W2.1-W2.6 fixed page words. */
export const SOFTWARE_PAGE = {
  title: 'Software updates',
  intro: 'Keeps the link box at each attendance machine on the right software.',
  menuIntro: 'Keep the link boxes on the right software',
  updateSoftware: 'Update software',
  noVersionReady: 'No version is ready to install.',
  tabsLabel: 'Software update sections',
  switchedOff: 'Updates for all devices are switched off for now. Ask IT to switch them on.',
  devicesFailed: "Couldn't load the device list.",
  catalogFailed: "Couldn't load software versions.",
  updatesFailed: "Couldn't refresh updates. Showing the list from before.",
  updatesTitle: 'Updates',
  updatesIntro: 'Open an update to see each device.',
  clearHistory: 'Clear history',
  clearHistoryHelp: 'Stops updates that have not started installing, and clears old ones. Records are kept.',
  hideFinished: 'Hide finished updates',
  noUpdatesMatch: 'No updates match.',
  noUpdates: 'No updates yet.',
  noUpdatesHelp: 'Press Update software to start one.',
  showOlderUpdates: 'Show older updates',
  versionsTitle: 'Software versions',
  versionsIntro: 'Every version, including old and withdrawn ones.',
  noVersionsMatch: 'No versions match.',
  noVersionsInUse: 'No versions in use.',
  noVersions: 'No software versions yet.',
  noVersionsHelp: 'New versions are added by IT.',
  noRunning: 'No updates are running.',
  noNewest: 'No version for all devices yet',
  setupLink: 'Set up a new device',
} as const

/** Plain toasts after one-click update actions. */
export const updateToast = {
  paused: (place: string) => `Update paused at ${place}.`,
  continuing: (place: string) => `Update continuing at ${place}.`,
  stopped: (place: string) => `Update stopped at ${place}. Start a new update to try again.`,
  hidden: (place: string) => `${place}: hidden from the list.`,
  hiddenMany: (count: number) => (count ? `${count} finished update${count === 1 ? '' : 's'} hidden.` : 'No finished updates to hide.'),
  started: (version: string, started: number, total: number) => `Version ${version} is starting on ${started} of ${total} device${total === 1 ? '' : 's'}.`,
  retried: (name: string) => `Trying again on ${name}.`,
  withdrawn: (version: string) => `Version ${version} withdrawn. Running updates that use it are paused.`,
} as const

/** "This device has been restarting a lot": one plain line per stability warning (W2.5). */
export function stabilityWarningLine(warning: { code: string; display_name?: string | null; history?: unknown; uptime_seconds?: unknown; interrupted_boots?: unknown }, name: string): string {
  const history = (warning.history ?? {}) as { resets?: number; days?: number }
  if (warning.code === 'ESP_RESET_RATE_HIGH' && typeof history.resets === 'number') {
    return `${name} restarted ${history.resets} time${history.resets === 1 ? '' : 's'} in the last ${history.days ?? 7} days. The update may be interrupted.`
  }
  if (warning.code === 'ESP_UPTIME_SHORT' && typeof warning.uptime_seconds === 'number') {
    const minutes = Math.floor(warning.uptime_seconds / 60)
    return `${name} restarted by itself ${minutes} minute${minutes === 1 ? '' : 's'} ago. The update may be interrupted.`
  }
  if (warning.code === 'TARGET_BOOT_INTERRUPTED_REPEATEDLY' && typeof warning.interrupted_boots === 'number') {
    return `${name}: the last ${warning.interrupted_boots} tries were interrupted. The update may be interrupted again.`
  }
  return `${name} has been restarting a lot. The update may be interrupted.`
}

/** Bulk start result words (W2.2). */
export const BULK_RESULT = {
  CREATED: 'Started',
  BUSY: 'Already updating',
  BLOCKED: 'Could not start',
} as const
