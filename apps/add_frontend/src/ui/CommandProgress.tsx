import { Icon } from '../Icon'
import { humanizeStatus, normalizedStatus, statusPattern } from '../status'
import type { Command } from '../types'
import { statusIcon } from './StatusBadge'

export function CommandProgress({
  command,
  onCancel,
}: {
  command: Command
  onCancel: (command: Command) => Promise<void>
}) {
  const status = normalizedStatus(command.status)
  const commandType = normalizedStatus(command.type)
  const canCancel = !['RUNNING', 'CANCEL_REQUESTED', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED'].includes(status)
  const pattern = statusPattern(status)
  return (
    <section className={`command-progress pattern-${pattern}`} aria-live="polite">
      <span className="command-symbol"><Icon name={status === 'SUCCEEDED' ? 'check' : status === 'FAILED' ? 'alert' : statusIcon[pattern]} /></span>
      <div>
        <p className="eyebrow">{commandType.replaceAll('_', ' ')}</p>
        <h3>{humanizeStatus(status)}</h3>
        <p>{command.error_message || (command.error_code ? humanizeStatus(command.error_code) : `Command ${command.command_id.slice(0, 8)} is durably tracked.`)}</p>
      </div>
      {canCancel && <button className="button secondary" onClick={() => void onCancel(command)}>Cancel before execution</button>}
    </section>
  )
}
