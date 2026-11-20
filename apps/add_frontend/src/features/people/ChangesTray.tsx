// Changes on this machine (07-people P9): one line per change with
// Waiting → Sent to machine → Confirmed, or Failed with Try again. Docked
// bottom-right on a computer, a bar above the bottom bar on a phone.
import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { Command } from '../../types'
import { Icon } from '../../Icon'
import { Steps, TechnicalDetails, useIsPhone, type StepsState } from '../../ui'
import { canCancelCommand, commandStep, useCommandTracker } from '../../lib/useCommandTracker'
import { clock } from '../../lib/time'
import { requestFailure } from '../../words/requests'
import { CHANGES, changeLabel, changeLine, peopleError } from '../../words/people'
import type { TrayEntry } from './useCommandTray'

export const CONFIRMED_FOLD_MS = 8_000

export function stepsState(command: Pick<Command, 'status'>): StepsState {
  const status = String(command.status || '').toUpperCase()
  if (status === 'EXPIRED') return 'timed-out'
  if (status === 'CANCEL_REQUESTED') return 'cancelling'
  const step = commandStep(command)
  return step === 'cancelled' ? 'cancelled' : step
}

/** Plain reason a change failed. */
export function changeFailure(command: Command): string {
  if (command.error_code) {
    const coded = requestFailure(command.error_code)
    if (!coded.unknown) return coded.sentence
  }
  return peopleError(command.error_message) || requestFailure(command.error_code).sentence
}

function TrayLine({ entry, revision, onUpdate, onFinal, onDismiss, onRetry, onCancelFailed }: {
  entry: TrayEntry
  revision: number
  onUpdate: (command: Command) => void
  onFinal: (entry: TrayEntry, command: Command) => void
  onDismiss: (id: string) => void
  onRetry?: (entry: TrayEntry) => void
  onCancelFailed: () => void
}) {
  const entryRef = useRef(entry)
  entryRef.current = entry
  const tracker = useCommandTracker(entry.command, revision, (command) => onFinal(entryRef.current, command))
  const command = tracker.command || entry.command
  useEffect(() => {
    if (tracker.command && tracker.command !== entry.command) onUpdate(tracker.command)
  }, [entry.command, onUpdate, tracker.command])
  const state = stepsState(command)
  useEffect(() => {
    if (state !== 'confirmed' && state !== 'cancelled') return
    const timer = window.setTimeout(() => onDismiss(entry.id), CONFIRMED_FOLD_MS)
    return () => window.clearTimeout(timer)
  }, [entry.id, onDismiss, state])
  const failed = state === 'failed' || state === 'timed-out'
  const reason = failed ? changeFailure(command) : ''
  const at = command.completed_at ? clock(command.completed_at) : ''
  return (
    <li className={`people-change is-${state}`} data-command-status={command.status}>
      <p className="people-change-label">{changeLabel(command.type, entry.name)}</p>
      <Steps state={state === 'timed-out' ? 'failed' : state} failedReason={state === 'timed-out' ? "the machine didn't answer in time" : reason.replace(/\.$/, '')} />
      <p className="people-change-line" aria-live="polite">{changeLine(command.status, reason, at)}</p>
      {tracker.checkFailed && <p className="people-change-line">{CHANGES.lostTouch}</p>}
      <div className="people-change-actions">
        {canCancelCommand(command) && (
          <button type="button" className="button secondary small" disabled={tracker.cancelling} onClick={() => { tracker.cancel().catch(onCancelFailed) }}>
            {tracker.cancelling ? 'Cancelling…' : CHANGES.cancel}
          </button>
        )}
        {failed && entry.retry && onRetry && <button type="button" className="button secondary small" onClick={() => onRetry(entry)}>{CHANGES.tryAgain}</button>}
        {(failed || state === 'confirmed' || state === 'cancelled') && (
          <button type="button" className="icon-button people-change-close" aria-label={CHANGES.close} onClick={() => onDismiss(entry.id)}><Icon name="x" size="sm" /></button>
        )}
      </div>
      {(failed || command.error_message) && (
        <TechnicalDetails>{[command.type, command.status, command.error_code, command.error_message, command.command_id].filter(Boolean).join(' · ')}</TechnicalDetails>
      )}
    </li>
  )
}

export function ChangesTray({ entries, revision, onUpdate, onFinal, onDismiss, onRetry, onCancelFailed, children, compact = false }: {
  entries: TrayEntry[]
  revision: number
  onUpdate: (command: Command) => void
  onFinal: (entry: TrayEntry, command: Command) => void
  onDismiss: (id: string) => void
  onRetry?: (entry: TrayEntry) => void
  onCancelFailed: () => void
  /** Extra card at the top (a removal of several people). */
  children?: ReactNode
  /** Select mode on a phone: a small chip in the header area. */
  compact?: boolean
}) {
  const phone = useIsPhone()
  const [open, setOpen] = useState(!phone)
  useEffect(() => { setOpen(!phone) }, [phone])
  if (!entries.length && !children) return null
  const sent = entries.filter((entry) => commandStep(entry.command) === 'sent').length
  const count = entries.length + (children ? 1 : 0)
  return (
    <section className={`people-changes ${open ? 'is-open' : ''} ${compact ? 'is-compact' : ''}`} aria-label={CHANGES.title}>
      <button type="button" className="people-changes-toggle" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        <span className="people-changes-title">{open ? CHANGES.title : CHANGES.summary(count, sent)}</span>
        <Icon name="chevronDown" size="sm" className="people-changes-chevron" />
      </button>
      <div className="people-changes-body" hidden={!open}>
        {children}
        <ul className="people-changes-list">
          {entries.map((entry) => (
            <TrayLine key={entry.id} entry={entry} revision={revision} onUpdate={onUpdate} onFinal={onFinal} onDismiss={onDismiss} onRetry={onRetry} onCancelFailed={onCancelFailed} />
          ))}
        </ul>
      </div>
    </section>
  )
}
