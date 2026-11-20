// Where a problem's "What to do" button goes (PRB-4, 05-problems PROBLEM_LINKS
// plus the C1 places in fe/words/problems.ts, which win). Every target is an
// in-app path opened through the router, never a full page load.
import { PROBLEM_LINKS as WORD_LINKS } from '../../words/problems'
import type { ProblemRow } from './types'

export type ProblemLink =
  | { kind: 'link'; label: string; to: string }
  | { kind: 'set-aside'; label: string }

const TABLE: Record<string, { label: string; to: string }> = {
  USER_SNAPSHOT_TRUNCATED: { label: 'Open People', to: '/people/{device}' },
  DUPLICATE_USER_CNIC: { label: 'Open People', to: '/people/{device}' },
  USER_CNIC_FORMAT_SUSPECT: { label: 'Open People', to: '/people/{device}' },
  USER_DELETION_JOB_INCOMPLETE: { label: 'Open People', to: '/people/{device}' },
  ADMIN_REVOKE_OVERDUE: { label: 'Open People', to: '/people/{device}' },
  ADD_SOURCE_COVERAGE_INVALIDATED: { label: 'Open Recover attendance', to: '/recover?tab=recoveries&device={zone}' },
  ADD_SOURCE_COVERAGE_UNVERIFIED: { label: 'Open Recover attendance', to: '/recover?tab=recoveries&device={zone}' },
  ADD_SOURCE_GENERATION_LINEAGE_DIVERGED: { label: 'Open Recover attendance', to: '/recover?tab=recoveries&device={zone}' },
  HIK_LIGHT_RECONCILE_BLOCKED: { label: 'Open Recover attendance', to: '/recover?tab=recoveries&device={zone}' },
  TERMINAL_NEWEST_RECORD_PUNCH_UNRECEIVED: { label: 'Open Recover attendance', to: '/recover?tab=recoveries&device={zone}' },
  TERMINAL_NEWEST_RECORD_RELEASE_HELD: { label: 'Open Recover attendance', to: '/recover?tab=recoveries&device={zone}' },
  OTA_DEVICE_REPORTED_FAILURE: { label: 'Open Software updates', to: '/updates?tab=updates&device={zone}' },
  OTA_DEVICE_ROLLED_BACK: { label: 'Open Software updates', to: '/updates?tab=updates&device={zone}' },
  ATTENDANCE_TIMESTAMP_QUARANTINED: { label: 'See these check-ins', to: '/attendance?device={device}&status=set_aside' },
  ORDS_EVENT_REJECTED: { label: 'See these check-ins', to: '/attendance?device={device}&status=problem' },
  HISTORY_BACKFILL_BLOCKED: { label: 'See these check-ins', to: '/attendance?device={device}&status=not_matched' },
  ATTENDANCE_REPAIR_NEEDS_ATTENTION: { label: 'Open attendance fix tools', to: '/attendance?view=fix-tools&tool=held-staff&device_id={device}' },
  ATTENDANCE_REPAIR_STALLED: { label: 'Open attendance fix tools', to: '/attendance?view=fix-tools&tool=held-staff&device_id={device}' },
  ZKT_SERIAL_MISMATCH: { label: 'Open device', to: '/devices/{device}?tab=tools&advanced=1' },
}

/**
 * Stored problems name the old address; both forms open Recover attendance ›
 * Unreadable records for that device (the allow-list from Monitoring.tsx,
 * widened by 08-advanced ADV-1). Anything else gives no link.
 */
const INSPECTOR = /^\/(?:reconciliation\?tab=source-exceptions|recover\?tab=unreadable)&device_id=([A-Za-z0-9-]{1,100})$/

export function inspectorPath(details: Record<string, unknown> | null | undefined): string | null {
  const value = details?.inspector_path
  if (typeof value !== 'string') return null
  const match = INSPECTOR.exec(value)
  return match ? `/recover?tab=unreadable&device_id=${match[1]}` : null
}

const fill = (template: string, row: ProblemRow) => template
  .replace('{device}', encodeURIComponent(row.device?.connector_id || ''))
  .replace('{zone}', encodeURIComponent(row.device?.zone_id || ''))

/** The one link button after "What to do", or null. */
export function problemLink(row: ProblemRow): ProblemLink | null {
  if (row.code === 'ATTENDANCE_EVENT_QUARANTINED') return { kind: 'set-aside', label: 'Check them' }
  const inspector = inspectorPath(row.details)
  if (inspector) return { kind: 'link', label: 'See the records', to: inspector }
  const entry = WORD_LINKS[row.code] || TABLE[row.code]
  if (!entry) return null
  if (entry.to.includes('{device}') && !row.device?.connector_id) return null
  if (entry.to.includes('{zone}') && !row.device?.zone_id) return null
  return { kind: 'link', label: entry.label, to: fill(entry.to, row) }
}

/** The device page for a problem: its Problems tab. */
export const devicePath = (connectorId: string) => `/devices/${encodeURIComponent(connectorId)}?tab=problems`
