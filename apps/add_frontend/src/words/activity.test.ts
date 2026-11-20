import { describe, expect, it } from 'vitest'
import { activitySummary, describeLog, logWords } from './activity'

describe('describeLog (Appendix B, simpler view)', () => {
  it('reads the live samples into plain sentences', () => {
    expect(describeLog({
      code: 'HIK_POLL_STATUS', level: 'INFO', subsystem: 'hikvision',
      message: 'Poll OK; reason=ok; stage=events; http=200; failures=0; terminal=192.168.18.31; duration_ms=258; cursor=180536; queued=0; storage=verified; source_worker=running',
    }).sentence).toBe('Checked the machine: all fine.')
    expect(describeLog({
      code: 'IDENTITY_CATALOG_APPLIED', level: 'INFO',
      message: 'Verified ADD identity catalog applied generation=2 rows=104 recovered=0; forcing authoritative truth reconciliation',
    }).sentence).toBe('Received the latest list of people (104) from the dashboard; checking records again.')
    expect(describeLog({
      code: 'CURRENT_TAIL_SOURCE_EXCEPTIONS_COMMITTED', level: 'WARN',
      message: 'Committed ordinals 2676..2677 with 1 fail-closed source exception(s)',
    }).sentence).toContain('1 unreadable record set aside')
  })

  it('adds the queued count and the memory note only when they apply', () => {
    expect(describeLog({ code: 'HIK_POLL_STATUS', level: 'INFO', message: 'Poll OK; queued=3; storage=unverified' }).sentence)
      .toBe('Checked the machine: all fine. 3 check-ins waiting to be sent. Link box memory not confirmed yet.')
    expect(describeLog({ code: 'HIK_POLL_STATUS', level: 'WARN', message: 'Poll retrying; reason=authentication_rejected; failures=2' }))
      .toMatchObject({ sentence: "Couldn't check the machine: the machine refused the password. Trying again.", act: 'if_repeats', next: 'Check the password saved for this machine.' })
  })

  it('never echoes text that is not a number', () => {
    expect(describeLog({ code: 'IDENTITY_CATALOG_APPLIED', level: 'INFO', message: 'rows=DROP TABLE' }).sentence)
      .toBe('Received the latest list of people from the dashboard; checking records again.')
  })

  it('marks the most frequent live ERROR as Routine, the serial mismatch as a Problem', () => {
    expect(describeLog({ code: 'ZKT_TIME_SYNC_READBACK_FAILED', level: 'ERROR' })).toMatchObject({ kind: 'routine', group: 'machine', act: 'if_repeats' })
    expect(describeLog({ code: 'ADD_MESSAGE_PROCESSING_FAILED', level: 'ERROR' })).toMatchObject({ kind: 'routine', groupLabel: 'Link box' })
    expect(describeLog({ code: 'ZKT_SERIAL_MISMATCH', level: 'CRITICAL' })).toMatchObject({ kind: 'problem', act: 'yes' })
  })

  it('uses the level variant when one exists', () => {
    expect(logWords('STORAGE_INVENTORY', 'WARN')).toMatchObject({ kind: 'problem', act: 'yes' })
    expect(logWords('STORAGE_REPAIR_DONE', 'ERROR')).toMatchObject({ sentence: 'Memory repair failed.', kind: 'problem' })
    expect(logWords('STORAGE_REPAIR_DONE', 'INFO')).toMatchObject({ kind: 'routine' })
  })

  it('reads the connection-lost reason and the light-check state', () => {
    expect(describeLog({ code: 'ZKT_CONNECTION_LOST', level: 'WARN', message: 'ZKT session ended before the stability window' }).sentence)
      .toBe('The connection dropped right after connecting. Trying again.')
    expect(describeLog({ code: 'HIK_LIGHT_RECONCILE', level: 'WARN', message: 'state=BLOCKED; error=7' })).toMatchObject({ kind: 'problem', act: 'yes' })
  })

  it('falls back by level and subsystem for unknown codes, never printing the code', () => {
    expect(describeLog({ code: 'NEW_FIRMWARE_CODE', level: 'INFO', subsystem: 'ords' }))
      .toMatchObject({ sentence: 'Other activity from the link box.', kind: 'routine', group: 'hr_system', unknown: true })
    expect(describeLog({ code: 'NEW_FIRMWARE_CODE', level: 'CRITICAL', subsystem: 'storage' }))
      .toMatchObject({ sentence: 'The link box reported a serious problem.', kind: 'problem', group: 'memory', next: 'Tell IT (details below).' })
    expect(describeLog({ code: 'ZKT_DIAG_SOMETHING', level: 'INFO' }).sentence).toBe('Test by IT finished; no settings were changed.')
  })
})

describe('activitySummary', () => {
  it('says how many problems and routine updates', () => {
    expect(activitySummary(0, 1214)).toBe('No problems today · 1,214 routine updates')
    expect(activitySummary(1, 214)).toBe('1 problem · 214 routine updates today')
    expect(activitySummary(2, 0, 14)).toBe('2 problems need attention · 14 fixed themselves')
  })
})
