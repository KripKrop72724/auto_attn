// Person sheet (07-people P5): the facts, one block per problem with numbered
// fix steps and the fix button, the actions with the reason written under any
// that isn't possible, and Technical details.
import { useState } from 'react'
import { Sheet, Steps, TechnicalDetails } from '../../ui'
import { Icon } from '../../Icon'
import type { CommandStep } from '../../lib/useCommandTracker'
import { dayTime } from '../../lib/time'
import { LIST, PROBLEM_HELP, SHEET, employeeIdList, personProblems } from '../../words/people'
import type { PeopleUser } from './model'

export interface PersonActions {
  edit: { allowed: boolean; reason: string }
  lease: { allowed: boolean; reason: string; shown: boolean }
  remove: { allowed: boolean; reason: string; shown: boolean }
}

function Reason({ text }: { text: string }) {
  return text ? <small className="people-action-reason">{text}</small> : null
}

export function PersonSheet({ user, actions, step, oldCount, onClose, onEdit, onLease, onRemove, onOpenSameCnic, onOpenOld }: {
  user: PeopleUser
  actions: PersonActions
  step: CommandStep | null
  oldCount: number
  onClose: () => void
  onEdit: () => void
  onLease: () => void
  onRemove: () => void
  onOpenSameCnic: () => void
  onOpenOld: () => void
}) {
  const [copied, setCopied] = useState(false)
  const problems = personProblems(user)
  const members = user.identity_conflict_members.map((member) => member.user_id)
  const copyId = async () => {
    try {
      await navigator.clipboard?.writeText(user.user_id)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch { /* copying is a convenience */ }
  }
  const blocks = problems.filter((problem) => ['no_cnic', 'cnic_typed', 'same_cnic', 'damaged'].includes(problem.key))
  return (
    <Sheet title={user.display_name} onClose={onClose} className="people-person-sheet">
      <div className="people-person-facts">
        <p>
          {LIST.employeeId(user.user_id)}{' '}
          <button type="button" className="text-button" onClick={() => void copyId()}><Icon name={copied ? 'check' : 'copy'} size="sm" /> {copied ? SHEET.copied : SHEET.copy}</button>
          {' · '}{user.privilege === 14 ? SHEET.machineAdmin : SHEET.regular}
        </p>
        <p>{user.cnic_masked ? `CNIC ${user.cnic_masked}` : SHEET.noCnic}</p>
        <p>{SHEET.worksInShifts(Boolean(user.shift_worker))}</p>
        {user.machine_name_preview && <p>{SHEET.nameOnMachine} <span data-user-text>{user.machine_name_preview}</span></p>}
      </div>
      {blocks.map((problem) => {
        if (problem.key === 'no_cnic') return (
          <section key={problem.key} className="people-problem-block">
            <h3><Icon name="alert" size="sm" /> {PROBLEM_HELP.no_cnic.title}</h3>
            <p>{PROBLEM_HELP.no_cnic.body}</p>
            <ol>{PROBLEM_HELP.no_cnic.steps.map((text) => <li key={text}>{text}</li>)}</ol>
            {actions.edit.allowed ? <button type="button" className="button primary" onClick={onEdit}>{SHEET.addCnic}</button> : <Reason text={actions.edit.reason} />}
          </section>
        )
        if (problem.key === 'cnic_typed') return (
          <section key={problem.key} className="people-problem-block">
            <h3><Icon name="alert" size="sm" /> {PROBLEM_HELP.cnic_typed.title}</h3>
            <p>{PROBLEM_HELP.cnic_typed.body}</p>
            <ol>{PROBLEM_HELP.cnic_typed.steps.map((text) => <li key={text}>{text}</li>)}</ol>
            {actions.edit.allowed ? <button type="button" className="button primary" onClick={onEdit}>{SHEET.fixCnic}</button> : <Reason text={actions.edit.reason} />}
          </section>
        )
        if (problem.key === 'same_cnic') return (
          <section key={problem.key} className="people-problem-block is-problem">
            <h3><Icon name="alert" size="sm" /> {PROBLEM_HELP.same_cnic.title(employeeIdList(members))}</h3>
            <p>{PROBLEM_HELP.same_cnic.body}</p>
            <ol>{PROBLEM_HELP.same_cnic.steps.map((text) => <li key={text}>{text}</li>)}</ol>
            <div className="people-sheet-buttons">
              <button type="button" className="button secondary" onClick={onOpenSameCnic}>{SHEET.openSameCnic}</button>
              {actions.edit.allowed && <button type="button" className="button primary" onClick={onEdit}>{SHEET.fixThisCnic}</button>}
            </div>
            {!actions.edit.allowed && <Reason text={actions.edit.reason} />}
          </section>
        )
        return (
          <section key={problem.key} className="people-problem-block is-problem">
            <h3><Icon name="alert" size="sm" /> {PROBLEM_HELP.damaged.title}</h3>
            <p>{PROBLEM_HELP.damaged.body}</p>
          </section>
        )
      })}
      {oldCount > 0 && (
        <section className="people-problem-block">
          <p>{PROBLEM_HELP.old(oldCount)}</p>
          <button type="button" className="button secondary" onClick={onOpenOld}>{SHEET.openOldCheckins}</button>
        </section>
      )}
      {(step || user.current_command_state) && (
        <section className="people-problem-block" aria-label={SHEET.changeTitle}>
          <h3>{SHEET.changeTitle}</h3>
          <Steps state={step === 'cancelled' ? 'cancelled' : step || 'waiting'} />
        </section>
      )}
      <div className="people-sheet-actions">
        <div>
          <button type="button" className="button secondary full" disabled={!actions.edit.allowed} onClick={onEdit}><Icon name="edit" /> {SHEET.edit}</button>
          {!actions.edit.allowed && <Reason text={actions.edit.reason} />}
        </div>
        {actions.lease.shown && (
          <div>
            <button type="button" className="button secondary full" disabled={!actions.lease.allowed} onClick={onLease}><Icon name="shield" /> {SHEET.adminAccess}</button>
            <Reason text={actions.lease.allowed ? '' : actions.lease.reason} />
          </div>
        )}
        {actions.remove.shown && (
          <div>
            <button type="button" className="button destructive-outline full" disabled={!actions.remove.allowed} onClick={onRemove}><Icon name="trash" /> {SHEET.remove}</button>
            <Reason text={actions.remove.allowed ? '' : actions.remove.reason} />
          </div>
        )}
      </div>
      <TechnicalDetails>
        {[
          `UID ${user.uid}`, `version ${user.row_version}`, `user_key ${user.user_key}`, `state ${user.lifecycle_state}`,
          user.identity_conflict_code ? `conflict ${user.identity_conflict_code}` : null,
          `observed ${dayTime(user.observed_at)}`, user.created_at ? `created ${dayTime(user.created_at)}` : null,
          user.machine_name_preview ? `machine name ${user.machine_name_preview}` : null,
        ].filter(Boolean).join(' · ')}
      </TechnicalDetails>
    </Sheet>
  )
}
