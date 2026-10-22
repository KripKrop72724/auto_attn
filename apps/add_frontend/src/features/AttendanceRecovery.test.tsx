import { describe, expect, it } from 'vitest'
import type { AttendanceRecoverySummary } from '../types'
import { withoutOutboxDetail } from './AttendanceRecovery'

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
  it('keeps held rows out of the missing count and names them separately', () => {
    expect(withoutOutboxDetail(delivery({ held_without_outbox: 75002, review_without_outbox: 143 })))
      .toBe(`Deliverable rows to rebuild · ${(75145).toLocaleString()} held rows have none by design`)
  })

  it('stays the plain rebuild label for an older backend or no held rows', () => {
    expect(withoutOutboxDetail(delivery({}))).toBe('Deliverable rows to rebuild')
    expect(withoutOutboxDetail(undefined)).toBe('Deliverable rows to rebuild')
  })
})
