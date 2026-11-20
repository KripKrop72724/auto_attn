// The people on one machine (07-people P4): a table on a computer, one card per
// person on a phone. The page scrolls (no inner scroll box); from 200 rows the
// list is virtualized against the workspace scroll element.
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { AnchoredLayer } from '../../AnchoredLayer'
import { Icon } from '../../Icon'
import { StatusChip, type Tone as ChipTone } from '../../ui'
import type { CommandStep } from '../../lib/useCommandTracker'
import { LIST, ROW_STEP, personInitials, personProblems, type PersonProblem } from '../../words/people'
import { needsFixing, type PeopleUser } from './model'
import type { PersonActions } from './PersonSheet'

const VIRTUAL_FROM = 200

const chipTone = (tone: PersonProblem['tone']): ChipTone =>
  tone === 'problem' ? 'offline' : tone === 'warning' ? 'attention' : tone === 'info' ? 'info' : tone === 'good' ? 'working' : 'neutral'

function stepChip(step: CommandStep | null, sending: boolean) {
  if (step === 'waiting') return <StatusChip tone="info" label={ROW_STEP.waiting} labelPrefix={null} size="sm" icon="clock" />
  if (step === 'sent') return <StatusChip tone="info" label={ROW_STEP.sent} labelPrefix={null} size="sm" icon="refresh" />
  if (step === 'confirmed') return <StatusChip tone="working" label={ROW_STEP.confirmed} labelPrefix={null} size="sm" />
  if (step === 'failed') return <StatusChip tone="offline" label={ROW_STEP.failed} labelPrefix={null} size="sm" />
  if (sending) return <StatusChip tone="info" label={ROW_STEP.sending} labelPrefix={null} size="sm" icon="refresh" />
  return null
}

function RowMenu({ user, actions, onLease, onRemove }: { user: PeopleUser; actions: PersonActions; onLease: () => void; onRemove: () => void }) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const run = (action: () => void) => { setOpen(false); triggerRef.current?.focus(); action() }
  if (!actions.lease.shown && !actions.remove.shown) return null
  return (
    <>
      <button ref={triggerRef} className="icon-button user-more-action" type="button" aria-label={LIST.moreFor(user.display_name)} aria-expanded={open} aria-haspopup="menu" onClick={() => setOpen((value) => !value)}><Icon name="more" /></button>
      {open && (
        <AnchoredLayer anchorRef={triggerRef} className="user-action-layer" mobileSheet preferredWidth={300} onDismiss={(reason) => { setOpen(false); if (reason === 'escape') triggerRef.current?.focus() }}>
          <div className="user-action-menu-panel" role="group" aria-label={LIST.optionsFor(user.display_name)}>
            {actions.lease.shown && <button type="button" disabled={!actions.lease.allowed} onClick={() => run(onLease)}><Icon name="shield" /><span><strong>{LIST.adminAccess}</strong><small>{actions.lease.allowed ? LIST.adminAccessHelp : actions.lease.reason}</small></span></button>}
            {actions.remove.shown && <button type="button" className="danger-action" aria-label={LIST.removePerson(user.display_name)} disabled={!actions.remove.allowed} onClick={() => run(onRemove)}><Icon name="trash" /><span><strong>{LIST.remove}</strong><small>{actions.remove.allowed ? LIST.removeHelp : actions.remove.reason}</small></span></button>}
          </div>
        </AnchoredLayer>
      )}
    </>
  )
}

export interface PeopleListProps {
  rows: PeopleUser[]
  phone: boolean
  /** Checkboxes, Edit and ⋯ only on a machine that can be changed. */
  writable: boolean
  canSelect: boolean
  selectMode: boolean
  selectedKeys: ReadonlySet<string>
  eligible: (user: PeopleUser) => boolean
  validating: boolean
  onToggle: (user: PeopleUser, checked: boolean) => void
  actionsFor: (user: PeopleUser) => PersonActions
  stepFor: (userKey: string) => CommandStep | null
  changedKeys: ReadonlySet<string>
  onOpen: (user: PeopleUser) => void
  onEdit: (user: PeopleUser) => void
  onLease: (user: PeopleUser) => void
  onRemove: (user: PeopleUser) => void
  busy: boolean
  footer?: ReactNode
}

export function PeopleList({
  rows, phone, writable, canSelect, selectMode, selectedKeys, eligible, validating, onToggle, actionsFor, stepFor, changedKeys,
  onOpen, onEdit, onLease, onRemove, busy, footer,
}: PeopleListProps) {
  const listRef = useRef<HTMLDivElement>(null)
  const [scrollMargin, setScrollMargin] = useState(0)
  const virtual = rows.length >= VIRTUAL_FROM
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => (typeof document === 'undefined' ? null : document.querySelector<HTMLElement>('.app-workspace')),
    estimateSize: () => (phone ? 132 : 72),
    overscan: 10,
    enabled: virtual,
    scrollMargin,
  })
  useLayoutEffect(() => {
    if (!virtual) return
    const workspace = document.querySelector<HTMLElement>('.app-workspace')
    const list = listRef.current
    if (!workspace || !list) return
    const margin = list.getBoundingClientRect().top - workspace.getBoundingClientRect().top + workspace.scrollTop
    if (Math.abs(margin - scrollMargin) > 1) setScrollMargin(margin)
  })
  useEffect(() => { virtualizer.measure() }, [phone, virtualizer])

  const showBoxes = writable && canSelect && (!phone || selectMode)
  const renderRow = (user: PeopleUser, style?: CSSProperties, index?: number) => {
    const problems = personProblems(user).filter((problem) => problem.key !== 'sending' && (phone || problem.key !== 'admin'))
    const fixing = needsFixing(user)
    const step = stepFor(user.user_key)
    const actions = actionsFor(user)
    const canTick = eligible(user) && actions.remove.allowed
    const name = user.display_name
    const chips = (
      <span className="people-row-chips">
        {problems.map((problem) => <StatusChip key={problem.key} tone={chipTone(problem.tone)} label={problem.label} labelPrefix={null} size="sm" icon={problem.key === 'admin' || problem.key === 'same_person' ? null : undefined} />)}
        {stepChip(step, Boolean(user.current_command_state))}
      </span>
    )
    return (
      <article
        key={user.user_key}
        ref={index == null ? undefined : virtualizer.measureElement}
        data-index={index}
        style={style}
        data-key={user.user_key}
        data-changed={changedKeys.has(user.user_key) ? '' : undefined}
        className={`user-directory-row ${user.identity_conflict_resolved ? 'identity-resolved' : user.identity_conflict_code ? 'identity-conflict' : user.identity_complete ? '' : 'identity-missing'} ${showBoxes ? 'has-select' : ''}`}
        data-pattern={user.identity_conflict_code && !user.identity_conflict_resolved ? 'blocked' : fixing ? 'waiting' : undefined}
        aria-label={LIST.rowLabel(name, user.user_id, fixing)}
      >
        {showBoxes && (
          <label className="user-select-hit">
            <input className="user-select" type="checkbox" aria-label={LIST.selectPerson(name)} checked={selectedKeys.has(user.user_key)} disabled={!canTick || validating} onChange={(event) => onToggle(user, event.target.checked)} />
          </label>
        )}
        <div className="people-cell people-cell-person">
          <span className="avatar" aria-hidden="true" data-user-text>{personInitials(name)}</span>
          <span className="people-person-text">
            <button type="button" className="people-person-open" onClick={() => onOpen(user)}>
              <strong data-user-text title={name.length > 40 ? name : undefined}>{name}</strong>
            </button>
            {phone && <small>{LIST.employeeId(user.user_id)}</small>}
            {phone && user.cnic_masked && <small>CNIC {user.cnic_masked}</small>}
            {user.shift_worker && <small>{LIST.worksInShifts}</small>}
          </span>
          {phone && <Icon name="chevron" size="sm" className="people-card-chevron" />}
        </div>
        {!phone && <div className="people-cell people-cell-id">{user.user_id}</div>}
        {!phone && <div className="people-cell people-cell-cnic">{user.cnic_masked || '—'}</div>}
        {!phone && <div className="people-cell people-cell-role">{user.privilege === 14 ? <StatusChip tone="neutral" label={LIST.machineAdmin} labelPrefix={null} size="sm" icon={null} /> : LIST.regular}</div>}
        <div className="people-cell people-cell-needs">{chips}</div>
        {!phone && (
          <div className="people-cell people-cell-actions user-row-actions">
            {writable && <button className="button secondary small user-edit-action" type="button" aria-label={LIST.editPerson(name)} disabled={!actions.edit.allowed} onClick={() => onEdit(user)}><Icon name="edit" /> {LIST.edit}</button>}
            {writable && <RowMenu user={user} actions={actions} onLease={() => onLease(user)} onRemove={() => onRemove(user)} />}
          </div>
        )}
      </article>
    )
  }

  return (
    <div ref={listRef} className={`user-directory-table ${virtual ? 'is-virtualized' : ''} ${phone ? 'is-cards' : 'is-table'} ${busy ? 'is-searching' : ''} ${showBoxes ? 'has-select' : ''}`} aria-busy={busy} aria-label={LIST.heading} role="region">
      {!phone && (
        <div className="user-directory-head" aria-hidden="true">
          {showBoxes && <span />}
          <span>{LIST.columns.person}</span><span>{LIST.columns.id}</span><span>{LIST.columns.cnic}</span><span>{LIST.columns.role}</span><span>{LIST.columns.needs}</span><span className="sr-only">{LIST.columns.actions}</span>
        </div>
      )}
      {!virtual && rows.map((user) => renderRow(user))}
      {virtual && (
        <div className="virtual-user-rows" style={{ height: virtualizer.getTotalSize() }}>
          {virtualizer.getVirtualItems().map((item) => renderRow(rows[item.index], { position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${item.start - scrollMargin}px)` }, item.index))}
        </div>
      )}
      {footer}
    </div>
  )
}
