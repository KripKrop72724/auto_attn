// The check-in list (ATT-9): one row per check-in with who, when, Check-in or
// Check-out, the place with its Life / Health tag, one status chip and one
// short note. The whole row opens the check-in sheet. Select mode adds 44 px
// checkboxes. A table from 900 px of list width, cards below (container query).
import { memo } from 'react'
import { Icon } from '../../Icon'
import { DivisionTag, StatusChip } from '../../ui'
import { CheckinNotifyFailedMark } from '../CheckinNotification'
import { checkinKind, GROUP_CHIP, personName, placeDivision, placeLine, rowGroup, rowTime, statusNote } from './attendanceWords'
import type { AttendanceRow as Row } from './types'

/** The `canSelect` rule (unchanged): a check-in that can be sent by hand. */
export const canSelect = (row: Row) => (row.direct_ords_identity?.eligible ?? Boolean(row.cnic_masked)) &&
  !row.oracle_confirmed_at && !row.ords_status.startsWith('ACK') &&
  row.ords_status !== 'IN_FLIGHT' && !row.force_release

export const rowName = (row: Row) => personName(row)
export const rowSummary = (row: Row, now?: number) => `${rowName(row)}, ${checkinKind(row.punch)} ${rowTime(row.device_event_time, now)}`

type RowProps = {
  row: Row
  now: number
  autoSending: boolean | null
  selectMode: boolean
  selected: boolean
  selectDisabled: boolean
  isNew: boolean
  changed: boolean
  onToggle: (row: Row) => void
  onOpen: (row: Row, trigger: HTMLElement) => void
}

export const AttendanceRowItem = memo(function AttendanceRowItem({ row, now, autoSending, selectMode, selected, selectDisabled, isNew, changed, onToggle, onOpen }: RowProps) {
  const group = rowGroup(row)
  const chip = GROUP_CHIP[group]
  const kind = checkinKind(row.punch)
  const time = rowTime(row.device_event_time, now)
  const name = rowName(row)
  const division = placeDivision(row)
  return (
    <li className="attendance-row" data-group={group} data-new={isNew || undefined} data-changed={changed || undefined}>
      <article aria-label={`${name}, ${kind}, ${time}, ${chip.label}`} className="attendance-row-body">
        {selectMode && (
          <label className="attendance-row-select">
            <input type="checkbox" aria-label={`Select ${row.display_name || `Employee ID ${row.user_id}`}, ${kind} ${time}`} checked={selected} disabled={selectDisabled} onChange={() => onToggle(row)} />
          </label>
        )}
        <button type="button" className="attendance-row-open" aria-label={`Open check-in ${name}, ${kind} ${time}`} onClick={(event) => onOpen(row, event.currentTarget)}>
          <span className="attendance-cell attendance-person">
            <strong data-user-text>{name}</strong>
            <small>Employee ID {row.user_id} · {kind}</small>
          </span>
          <span className="attendance-cell attendance-time"><strong>{time}</strong><small>{kind}</small></span>
          <span className="attendance-cell attendance-place">
            <span>{placeLine(row)}</span>
            {division && <DivisionTag division={division} />}
          </span>
          <span className="attendance-cell attendance-status">
            <StatusChip tone={chip.tone} label={chip.label} icon={chip.icon} code={row.ords_status} />
            <small>{statusNote(row, autoSending, now)}</small>
            <CheckinNotifyFailedMark row={row as never} />
          </span>
          <Icon name="chevron" size="sm" className="attendance-row-chevron" />
        </button>
        {isNew && <span className="attendance-new-tag">New</span>}
      </article>
    </li>
  )
})

export function AttendanceListSkeleton() {
  return (
    <div className="attendance-skeleton" role="status" aria-label="Loading check-ins">
      {Array.from({ length: 6 }, (_, index) => <span key={index}><i /><i /><i /></span>)}
    </div>
  )
}
