// Page-local shapes of GET /api/v1/alerts (PRB-1/PRB-2 read-side fields). Kept
// here instead of fe/types.ts so the shared type file stays untouched; every
// new field is optional, so an older backend still type-checks and renders.
import type { Alert, HealthOperatorPolicy } from '../../types'

export type ProblemDivision = 'life' | 'health'

export interface ProblemDevice {
  connector_id: string
  display_name: string
  zone_id: string
  hardware_id?: string | null
  division?: ProblemDivision | null
  friendly_name?: string | null
}

export interface ProblemRow extends Alert {
  plain_title?: string | null
  plain_detail?: string | null
  next_step?: string | null
  type?: string | null
  affects_attendance?: boolean | null
  operator?: HealthOperatorPolicy | null
  device?: ProblemDevice
}

export interface ProblemCounts {
  open: number
  seen: number
  legacy_seen: number
  fixed: number
  by_division?: Partial<Record<ProblemDivision, { open: number; seen: number; fixed: number }>>
  by_severity?: Partial<Record<'CRITICAL' | 'HIGH' | 'WARNING' | 'MEDIUM' | 'INFO', number>>
  by_type?: Partial<Record<string, number>>
  legacy_by_code?: Record<string, number>
}

export interface ProblemsResponse {
  rows: ProblemRow[]
  next_cursor: string | null
  totals?: { all: number; open: number; acknowledged: number; resolved: number }
  queue_totals?: { needs_action: number; acknowledged: number; resolved: number; all: number }
  counts?: ProblemCounts
}
