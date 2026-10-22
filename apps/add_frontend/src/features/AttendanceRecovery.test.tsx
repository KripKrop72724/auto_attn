import { describe, expect, it } from 'vitest'
import type { AttendanceRecoverySummary } from '../types'
import { withoutOutboxDetail, withoutOutboxTone } from './AttendanceRecovery'

const delivery = (overrides: Partial<AttendanceRecoverySummary['delivery']>): AttendanceRecoverySummary['delivery'] => ({
  safe_retryable: 0,
  missing_outbox: 0,
  stale_in_flight: 0,
  identity_held: 0,
  permanent_review: 0,
  status_counts: {},
  ...overrides,
})

describe('missing outbox detail', () => {
  it('names held and review rows apart and keeps both out of the missing count', () => {
    expect(withoutOutboxDetail(delivery({ held_without_outbox: 74838, review_without_outbox: 307 })))
      .toBe(`Deliverable rows to rebuild · ${(74838).toLocaleString()} held · ${(307).toLocaleString()} for review`)
  })

  it('never calls review rows held', () => {
    expect(withoutOutboxDetail(delivery({ held_without_outbox: 0, review_without_outbox: 3 })))
      .toBe('Deliverable rows to rebuild · 3 for review')
    expect(withoutOutboxDetail(delivery({ held_without_outbox: 5, review_without_outbox: 0 })))
      .toBe('Deliverable rows to rebuild · 5 held')
    expect(withoutOutboxDetail(delivery({ held_without_outbox: 0, review_without_outbox: 0 })))
      .toBe('Deliverable rows to rebuild')
  })

  it('keeps the original label when an older backend still counts held rows as missing', () => {
    expect(withoutOutboxDetail(delivery({ missing_outbox: 75145 }))).toBe('Durable rows to rebuild')
    expect(withoutOutboxDetail(undefined)).toBe('Durable rows to rebuild')
  })
})

describe('missing outbox tone', () => {
  it('stays positive when only held or review rows lack an outbox', () => {
    expect(withoutOutboxTone(delivery({ held_without_outbox: 74838, review_without_outbox: 0 }))).toBe('positive')
    // ZKT invalid-time quarantines never get an outbox; the Source review tile
    // already flags them, so they must not keep this tile amber for good.
    expect(withoutOutboxTone(delivery({ held_without_outbox: 74838, review_without_outbox: 143 }))).toBe('positive')
    expect(withoutOutboxTone(undefined)).toBe('positive')
  })

  it('turns amber for a real gap, whatever else lacks an outbox', () => {
    expect(withoutOutboxTone(delivery({ missing_outbox: 1 }))).toBe('warning')
    expect(withoutOutboxTone(delivery({ missing_outbox: 1, held_without_outbox: 74838, review_without_outbox: 143 }))).toBe('warning')
  })
})
