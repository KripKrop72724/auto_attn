// Attendance page types that the shared types.ts does not have yet (UI/UX
// revamp ATT-3..ATT-5 read-side fields). Kept here so this page builds alone;
// the integrator may move them into types.ts.
import type { AttendanceEvent } from '../../types'
import type { DeliveryGroup } from '../../words/attendance'

export type { DeliveryGroup } from '../../words/attendance'

export type AttendanceRow = Omit<AttendanceEvent, 'source_connector'> & {
  /** sent / waiting / not_matched / set_aside / problem (backend `attendance_groups`). */
  delivery_group?: DeliveryGroup | string | null
  source_connector?: (NonNullable<AttendanceEvent['source_connector']> & {
    division?: 'life' | 'health' | null
    friendly_name?: string | null
  }) | null
}

export type AttendanceListResponse = {
  rows: AttendanceRow[]
  next_cursor: number | null
  status_options?: string[]
}

export type GroupTotals = {
  received: number
  sent: number
  waiting: number
  trying_again: number
  not_matched: number
  set_aside: number
  problem: number
  oldest_waiting_at?: string | null
}

export type AttendanceSummary = {
  from_time: string
  to_time: string | null
  division?: 'life' | 'health' | null
  computed_at: string
  max_age_seconds: number
  totals: GroupTotals
  last_sent_at: string | null
  by_division: { life: GroupTotals; health: GroupTotals }
}

export type JourneyStep = { step: 'checked_in' | 'reached_dashboard' | 'queued_for_hr' | 'sent_to_hr' | string; at: string | null; done: boolean }

export type AttendanceJourney = {
  id: number
  delivery_group: DeliveryGroup | string
  ords_status: string
  source: string
  live: boolean
  device_event_time: string | null
  captured_at: string | null
  received_at: string | null
  oracle_confirmed_at: string | null
  outbox: {
    status: string
    delivery_type: string | null
    created_at: string | null
    last_attempt_at: string | null
    attempt_count: number
    next_attempt_at: string | null
    acknowledged_at: string | null
  } | null
  steps: JourneyStep[]
}

/** The overview's all-time figures (cached `/api/v1/overview` section). */
export type AttendanceAllTime = GroupTotals & { since: string | null; range?: string }
