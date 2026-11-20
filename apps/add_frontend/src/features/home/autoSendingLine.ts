// The automatic-sending line inside "Attendance today" (03-home-devices Words,
// replaces the old "Unknown-identity release" tile). The switch itself and its
// password + reason stay on the Attendance page, unchanged.
import { formatCount } from '../../words/glossary'
import { formatAgo, remainingOf, secondsSince, type AutoReleaseStatus } from '../unknownAutoRelease'

export function autoSendingLine(status: AutoReleaseStatus): { text: string; tone: 'quiet' | 'warning' } {
  const remaining = remainingOf(status)
  const left = remaining === null ? 'still being counted' : `${formatCount(remaining)} left`
  if (!status.allowed) return { text: 'Automatic sending of not-matched check-ins is not allowed on this system.', tone: 'quiet' }
  if (status.enabled) return { text: `Sending not-matched check-ins automatically: On · ${left}.`, tone: 'quiet' }
  if (status.drain_active) return { text: `Sending all not-matched check-ins once: ${left}.`, tone: 'quiet' }
  const when = status.changed_at ? ` ${formatAgo(secondsSince(status.changed_at, status.server_time))}` : ''
  const who = status.changed_by ? ` by ${status.changed_by}` : ''
  const waiting = remaining === null ? 'still being counted' : `${formatCount(remaining)} waiting`
  return { text: `Sending not-matched check-ins automatically: Off (turned off${who}${when}) · ${waiting}.`, tone: 'warning' }
}
