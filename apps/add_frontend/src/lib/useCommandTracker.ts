// Follows one change sent to a machine (a command) until it ends: polls
// GET /api/v2/commands/{id} every 2 s, and at once when a `command` live event
// arrives (pass the realtime revision). Shared by the device page and People.
// Steps the person sees: Waiting → Sent to machine → Confirmed, or Didn't work.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { Command } from '../types'

export const COMMAND_POLL_MS = 2_000

export type CommandStep = 'waiting' | 'sent' | 'confirmed' | 'failed' | 'cancelled'

const WAITING = new Set(['QUEUED', 'WAITING_FOR_DEVICE', 'WAITING_FOR_ZKT', 'RETRYING', 'UNKNOWN', ''])
const SENT = new Set(['DISPATCHED', 'ACKNOWLEDGED', 'RUNNING', 'CANCEL_REQUESTED'])
export const FINAL_COMMAND_STATES = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'CANCELED', 'EXPIRED'])

const statusOf = (command: Pick<Command, 'status'> | null | undefined) => String(command?.status || '').toUpperCase()

/** Which of the three steps a command status belongs to. */
export function commandStep(command: Pick<Command, 'status'> | null | undefined): CommandStep {
  const status = statusOf(command)
  if (status === 'SUCCEEDED') return 'confirmed'
  if (status === 'FAILED' || status === 'EXPIRED') return 'failed'
  if (status === 'CANCELLED' || status === 'CANCELED') return 'cancelled'
  if (SENT.has(status)) return 'sent'
  return 'waiting'
}

export const isFinalCommand = (command: Pick<Command, 'status'> | null | undefined) => FINAL_COMMAND_STATES.has(statusOf(command))

/** A change can be cancelled only before the machine starts it. */
export const canCancelCommand = (command: Pick<Command, 'status'> | null | undefined) =>
  Boolean(command) && WAITING.has(statusOf(command)) && statusOf(command) !== ''

export interface CommandTracker {
  command: Command | null
  step: CommandStep | null
  final: boolean
  /** The last check could not reach the dashboard; polling carries on. */
  checkFailed: boolean
  cancelling: boolean
  cancel: () => Promise<void>
  refresh: () => void
}

/**
 * Tracks a command. `seed` is the command the dashboard answered with (or just
 * its id); `revision` should change on every `command` live event so the step
 * moves without waiting for the next poll.
 */
export function useCommandTracker(seed: Command | string | null | undefined, revision = 0, onFinal?: (command: Command) => void): CommandTracker {
  const commandId = typeof seed === 'string' ? seed : seed?.command_id || ''
  const [command, setCommand] = useState<Command | null>(typeof seed === 'object' && seed ? seed : null)
  const [checkFailed, setCheckFailed] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [tick, setTick] = useState(0)
  const onFinalRef = useRef(onFinal)
  onFinalRef.current = onFinal
  const reported = useRef('')
  const final = isFinalCommand(command)

  useEffect(() => {
    setCommand(typeof seed === 'object' && seed ? seed : null)
    setCheckFailed(false)
    reported.current = ''
    // A new command id starts a new tracker; the seed object itself may be re-created each render.
  }, [commandId])

  useEffect(() => {
    if (!commandId || final) return
    const controller = new AbortController()
    let timer = 0
    const check = async () => {
      try {
        const next = await api<Command>(`/api/v2/commands/${encodeURIComponent(commandId)}`, { signal: controller.signal })
        if (controller.signal.aborted) return
        setCommand(next)
        setCheckFailed(false)
        if (!isFinalCommand(next)) timer = window.setTimeout(() => void check(), COMMAND_POLL_MS)
      } catch (error) {
        if (controller.signal.aborted || (error instanceof Error && error.name === 'AbortError')) return
        setCheckFailed(true)
        timer = window.setTimeout(() => void check(), COMMAND_POLL_MS)
      }
    }
    // The first check waits one interval when the seed is fresh; a live event checks at once.
    timer = window.setTimeout(() => void check(), command && tick === 0 && revision === 0 ? COMMAND_POLL_MS : 0)
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
    // `command` is read only for the first delay; including it would restart polling on every answer.
  }, [commandId, final, revision, tick])

  useEffect(() => {
    if (!command || !final || reported.current === command.command_id) return
    reported.current = command.command_id
    onFinalRef.current?.(command)
  }, [command, final])

  const cancel = useCallback(async () => {
    if (!commandId) return
    setCancelling(true)
    try {
      const next = await api<Command>(`/api/v2/commands/${encodeURIComponent(commandId)}/cancel`, { method: 'POST', body: '{}' })
      setCommand(next)
    } finally {
      setCancelling(false)
    }
  }, [commandId])

  const refresh = useCallback(() => setTick((value) => value + 1), [])

  return { command, step: command ? commandStep(command) : commandId ? 'waiting' : null, final, checkFailed, cancelling, cancel, refresh }
}
