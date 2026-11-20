// Every change sent to one machine from the People page, each followed until it
// ends (07-people P9). Several can run at once; nothing is lost when a second
// one starts. The polling itself is lib/useCommandTracker, one per entry.
import { useCallback, useMemo, useState } from 'react'
import type { Command } from '../../types'
import { commandStep, isFinalCommand, type CommandStep } from '../../lib/useCommandTracker'
import type { UserDialogState } from '../../lib/dashboardTypes'

/** What "Try again" reopens: the same dialog with the values kept and the password empty. */
export type RetryState = { dialog: Exclude<UserDialogState, null>; draft?: Record<string, unknown> }

export interface TrayEntry {
  id: string
  command: Command
  /** Person this change is for (row chip), when there is one. */
  userKey?: string
  name?: string
  retry?: RetryState
}

export const TRAY_LIMIT = 10

export interface CommandTray {
  entries: TrayEntry[]
  /** Step per person (user_key) for the row chips. */
  rowSteps: Map<string, CommandStep>
  track: (command: Command, meta?: Omit<TrayEntry, 'id' | 'command'>) => void
  update: (command: Command) => void
  dismiss: (id: string) => void
  clear: () => void
}

export function useCommandTray(): CommandTray {
  const [entries, setEntries] = useState<TrayEntry[]>([])

  const track = useCallback((command: Command, meta: Omit<TrayEntry, 'id' | 'command'> = {}) => {
    if (!command?.command_id) return
    setEntries((current) => {
      const existing = current.find((entry) => entry.id === command.command_id)
      if (existing) return current.map((entry) => entry.id === command.command_id ? { ...entry, ...meta, command: isFinalCommand(entry.command) ? entry.command : command } : entry)
      const next = [...current, { id: command.command_id, command, ...meta }]
      if (next.length <= TRAY_LIMIT) return next
      // Drop the oldest finished entries first, then the oldest of all.
      const finishedIndex = next.findIndex((entry) => isFinalCommand(entry.command))
      return next.filter((_, index) => index !== (finishedIndex >= 0 ? finishedIndex : 0))
    })
  }, [])

  const update = useCallback((command: Command) => {
    setEntries((current) => current.some((entry) => entry.id === command.command_id && entry.command !== command)
      ? current.map((entry) => entry.id === command.command_id ? { ...entry, command } : entry)
      : current)
  }, [])

  const dismiss = useCallback((id: string) => setEntries((current) => current.filter((entry) => entry.id !== id)), [])
  const clear = useCallback(() => setEntries([]), [])

  const rowSteps = useMemo(() => {
    const steps = new Map<string, CommandStep>()
    for (const entry of entries) if (entry.userKey) steps.set(entry.userKey, commandStep(entry.command))
    return steps
  }, [entries])

  return { entries, rowSteps, track, update, dismiss, clear }
}
