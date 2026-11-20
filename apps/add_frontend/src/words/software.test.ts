import { describe, expect, it } from 'vitest'
import { findPlainWordProblems } from './glossary'
import {
  RETRY_REFUSALS, SOFTWARE_PAGE, UPDATE_FAILURES, UPDATE_FAILURE_FALLBACK, UPDATE_SHOW, UPDATE_STEPS, compareVersions, pauseSentence,
  plainVersion, readPause, retryRefusalSentence, softwareCounts, softwareStatus, stabilityWarningLine, updateChip, updateFailureSentence,
  updateNeedsYou, type LiveDeployment, type SoftwareContext, type SoftwareDevice,
} from './software'

const device = (changes: Partial<SoftwareDevice> = {}): SoftwareDevice => ({
  connector_id: 'c-1', firmware_version: 'zone-lite-2.7.8', firmware_family: 'zkt', ota_capable: true, ota_state: 'OTA_READY', ...changes,
})
const releases = [
  { version: '2.7.8', state: 'AVAILABLE', firmware_family: 'zkt' },
  { version: '2.7.6', state: 'AVAILABLE', firmware_family: 'zkt' },
  { version: '3.1.8', state: 'AVAILABLE', firmware_family: 'hikvision' },
  { version: '2.8.0', state: 'HIL_ONLY', firmware_family: 'zkt' },
]
const deployment = (changes: Partial<LiveDeployment>): LiveDeployment => ({
  connector_id: 'c-1', status: 'DOWNLOADING', target_version: '2.7.8', previous_version: 'zone-lite-2.7.6', bytes_written: 450, image_size: 1000, ...changes,
})
const ctx = (changes: Partial<SoftwareContext> = {}): SoftwareContext => ({ devicesLoaded: true, releases, liveDeployments: [], ...changes })

describe('softwareStatus(): the one rule', () => {
  it('covers every row of the rule table, in order', () => {
    expect(softwareStatus(device(), ctx({ devicesLoaded: false })).key).toBe('loading')
    expect(softwareStatus(device({ is_spare: true }), ctx()).key).toBe('spare')
    const updating = softwareStatus(device({ firmware_version: '2.7.6' }), ctx({ liveDeployments: [deployment({})] }))
    expect(updating).toMatchObject({ key: 'updating', label: 'Updating · Downloading 45%', short: '2.7.6 → 2.7.8 · Updating', chip: 'info', percent: 45 })
    expect(softwareStatus(device(), ctx({ liveDeployments: [deployment({ status: 'PENDING' })] }))).toMatchObject({ key: 'waiting', label: 'Waiting its turn' })
    const failed = softwareStatus(device(), ctx({ liveDeployments: [deployment({ status: 'ROLLED_BACK', error_code: 'TARGET_BOOT_INTERRUPTED' })] }))
    expect(failed).toMatchObject({ key: 'failed', label: 'Update failed', chip: 'offline' })
    expect(failed.sentence).toBe(UPDATE_FAILURES.TARGET_BOOT_INTERRUPTED)
    expect(softwareStatus(device({ ota_state: 'ROLLBACK_REQUIRED' }), ctx()).key).toBe('failed')
    expect(softwareStatus(device({ ota_state: 'OTA_BLOCKED' }), ctx())).toMatchObject({ key: 'blocked', label: "Can't update remotely right now" })
    expect(softwareStatus(device({ ota_capable: false }), ctx())).toMatchObject({ key: 'technician', short: 'Technician needed' })
    expect(softwareStatus(device({ ota_state: 'LEGACY_MANUAL_UPDATE' }), ctx()).key).toBe('technician')
    expect(softwareStatus(device({ firmware_version: null }), ctx())).toMatchObject({ key: 'unknown', label: 'Version not known yet' })
    expect(softwareStatus(device(), ctx())).toMatchObject({ key: 'current', label: 'Up to date', short: '2.7.8 · Up to date', chip: 'working' })
    expect(softwareStatus(device({ firmware_version: 'zone-lite-2.7.6' }), ctx())).toMatchObject({ key: 'newer', short: '2.7.6 · Newer available', chip: 'info' })
  })

  it('never says Up to date without a report or for an unconfirmed update', () => {
    expect(softwareStatus(device({ ota_state: undefined, firmware_version: null }), ctx()).key).not.toBe('current')
    expect(softwareStatus(device({ ota_state: 'UPDATING' }), ctx())).toMatchObject({ key: 'unconfirmed', label: 'Update status not confirmed yet', tone: 'warning' })
    // No catalog yet: the version is shown, no claim is made.
    expect(softwareStatus(device(), ctx({ releases: null }))).toMatchObject({ key: 'version', label: 'Version 2.7.8' })
  })

  it('uses the newest version of the same machine brand', () => {
    expect(softwareStatus(device({ firmware_family: 'hikvision', firmware_version: 'zone-lite-3.1.8' }), ctx()).key).toBe('current')
    expect(softwareStatus(device({ firmware_family: 'hikvision', firmware_version: 'zone-lite-3.1.5' }), ctx()).key).toBe('newer')
  })

  it('a test version above the newest, or made for this device, is Up to date', () => {
    expect(softwareStatus(device({ firmware_version: 'zone-lite-2.8.0' }), ctx()).key).toBe('current')
    expect(softwareStatus(device({ firmware_version: '2.6.0' }), ctx({ testVersions: new Map([['c-1', ['2.6.0']]]) })).key).toBe('current')
  })

  it('counts leave spares out and say how many', () => {
    const devices = [device(), device({ connector_id: 'c-2', firmware_version: '2.7.6' }), device({ connector_id: 'c-3', is_spare: true }), device({ connector_id: 'c-4', ota_capable: false })]
    expect(softwareCounts(devices, ctx())).toMatchObject({ total: 3, spares: 1, current: 1, newer: 1, technician: 1 })
    expect(softwareCounts(devices, ctx({ devicesLoaded: false })).total).toBe(0)
  })

  it('one fixture gives the same words for a list column and the status line', () => {
    const row = device({ firmware_version: 'zone-lite-2.7.6' })
    const column = softwareStatus(row, ctx()).short
    const counts = softwareCounts([row], ctx())
    expect(column).toBe('2.7.6 · Newer available')
    expect(counts.newer).toBe(1)
    expect(counts.current).toBe(0)
  })
})

describe('versions', () => {
  it('reads and compares versions number by number', () => {
    expect(plainVersion('zone-lite-2.7.8')).toBe('2.7.8')
    expect(plainVersion('ZKT Zone Lite 2.7.10')).toBe('2.7.10')
    expect(plainVersion(null)).toBeNull()
    expect(compareVersions('2.7.10', '2.7.9')).toBeGreaterThan(0)
    expect(compareVersions('zone-lite-2.7.8', '2.7.8')).toBe(0)
    expect(compareVersions('unknown', '2.7.8')).toBeNull()
  })
})

describe('update cards', () => {
  it('turns the stored pause reason into a plain sentence', () => {
    const pause = readPause({ status: 'PAUSED', pause_reason: 'beb5f8eb-0000-4000-8000-000000000000: ROLLED_BACK (TARGET_BOOT_INTERRUPTED)' })
    expect(pause).toMatchObject({ kind: 'DEVICE_ROLLED_BACK', code: 'TARGET_BOOT_INTERRUPTED', connectorId: 'beb5f8eb-0000-4000-8000-000000000000' })
    expect(pauseSentence(pause, { device: 'Multan · Device 2' })).toBe(
      'Paused because Multan · Device 2 could not update: The link box restarted before it finished installing and went back to the old version. Attendance is safe.',
    )
    expect(pauseSentence(readPause({ status: 'PAUSED', pause_reason: 'Paused with one click' }), { actor: 'Asad' })).toBe('Paused by Asad.')
    expect(readPause({ status: 'PAUSED', pause_reason: 'Release revoked: bad build on site' })).toMatchObject({ kind: 'RELEASE_REVOKED', note: 'bad build on site' })
    // The server's own fields win (ADV-17).
    expect(readPause({ status: 'PAUSED', pause_reason: 'x', pause_kind: 'DEVICE_FAILED', pause_code: 'PARTITION_HASH_FAILED', pause_device_name: 'Swat' }))
      .toMatchObject({ kind: 'DEVICE_FAILED', code: 'PARTITION_HASH_FAILED', deviceName: 'Swat' })
    // An unknown format is "Paused", never a crash.
    expect(pauseSentence(readPause({ status: 'PAUSED', pause_reason: 'something new' }), {})).toBe('Paused.')
  })

  it('chips say who needs to act', () => {
    expect(updateChip({ status: 'ACTIVE' })).toMatchObject({ label: 'Updating', chip: 'info' })
    expect(updateChip({ status: 'ACTIVE', waiting: { code: 'NATIONWIDE_TARGET_OFFLINE' } }).label).toBe('Waiting for device')
    expect(updateChip({ status: 'PAUSED', pause_reason: 'c: FAILED (IMAGE_TOO_LARGE)' })).toMatchObject({ label: 'Waiting for you', chip: 'offline' })
    expect(updateChip({ status: 'PAUSED', pause_reason: 'Paused with one click' }).label).toBe('Paused')
    expect(updateChip({ status: 'COMPLETED' }).label).toBe('Finished')
    expect(updateChip({ status: 'CANCELLED' }).label).toBe('Stopped')
    expect(updateChip({ status: 'CANCELLED', archived_at: '2026-11-01T00:00:00Z' }).label).toBe('Hidden')
    expect(updateNeedsYou({ status: 'ACTIVE', counts: { FAILED: 1 } })).toBe(true)
  })

  it('failure codes always read as a sentence; waits read as final checks', () => {
    expect(updateFailureSentence('BOOT_LOCAL_JOURNAL_RECOVERY')).toBe('Final checks after restart.')
    expect(updateFailureSentence('WAITING_FOR_RUNTIME_HEALTH')).toBe('Final checks after restart.')
    expect(updateFailureSentence('SOMETHING_NEW', 'FAILED')).toBe(UPDATE_FAILURE_FALLBACK)
    expect(updateFailureSentence('BOOT_HEALTH_TIMEOUT')).toMatch(/start-up checks/)
  })

  it('retry refusals are one plain sentence each', () => {
    expect(retryRefusalSentence('The ESP is offline.')).toBe('This device is offline; try when it is back.')
    expect(retryRefusalSentence('The ESP reports storage I/O faults on its current boot (write_failures=3); recover its storage before another attempt.'))
      .toBe('This device has a storage problem; fix that first.')
    expect(retryRefusalSentence('new server text')).toBe("This device can't try again right now.")
  })

  it('stability warnings are composed from the fields', () => {
    expect(stabilityWarningLine({ code: 'ESP_RESET_RATE_HIGH', history: { resets: 46, days: 7 } }, 'Multan · Device 2'))
      .toBe('Multan · Device 2 restarted 46 times in the last 7 days. The update may be interrupted.')
    expect(stabilityWarningLine({ code: 'ESP_UPTIME_SHORT', uptime_seconds: 720 }, 'Swat')).toBe('Swat restarted by itself 12 minutes ago. The update may be interrupted.')
  })
})

describe('page words follow the plain-words rules', () => {
  it('has no system words, codes or bare brand names', () => {
    const texts = [
      ...Object.values(UPDATE_FAILURES), ...Object.values(UPDATE_STEPS), ...Object.values(UPDATE_SHOW), ...Object.values(SOFTWARE_PAGE),
      ...RETRY_REFUSALS.map((row) => row.sentence), UPDATE_FAILURE_FALLBACK,
    ]
    const problems = texts.flatMap((text) => findPlainWordProblems(text).map((problem) => `${text} → ${problem.match}`))
    expect(problems).toEqual([])
  })
})
