import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Command } from '../../types'
import { ChangesTray, stepsState } from './ChangesTray'
import { useCommandTray } from './useCommandTray'

const cmd = (id: string, status: string, type = 'UPDATE_USER', extra: Partial<Command> = {}): Command => ({
  command_id: id, type, status, created_at: '2026-11-20T05:00:00Z', expires_at: null, ...extra,
})

const respond = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

function Harness({ seeds, onFinal = () => {}, onRetry }: { seeds: Array<{ command: Command; name?: string; retry?: boolean }>; onFinal?: (command: Command) => void; onRetry?: () => void }) {
  const tray = useCommandTray()
  return (
    <>
      <button type="button" onClick={() => seeds.forEach((seed) => tray.track(seed.command, { name: seed.name, userKey: seed.name, retry: seed.retry ? { dialog: { mode: 'create' }, draft: { displayName: seed.name } } : undefined }))}>start</button>
      <ChangesTray entries={tray.entries} revision={1} onUpdate={tray.update} onFinal={(_entry, command) => onFinal(command)} onDismiss={tray.dismiss} onRetry={onRetry ? () => onRetry() : undefined} onCancelFailed={() => {}} />
      <output data-testid="steps">{[...tray.rowSteps.entries()].map(([key, step]) => `${key}:${step}`).join(',')}</output>
    </>
  )
}

describe('Changes on this machine', () => {
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it.each([
    ['QUEUED', 'waiting', 'Saved. Waiting to send.'],
    ['WAITING_FOR_DEVICE', 'waiting', 'Waiting for the link box to come online.'],
    ['WAITING_FOR_ZKT', 'waiting', 'Waiting for the machine to answer.'],
    ['RETRYING', 'waiting', 'Trying again.'],
    ['DISPATCHED', 'sent', 'The machine is making the change.'],
    ['RUNNING', 'sent', 'The machine is making the change.'],
    ['CANCEL_REQUESTED', 'cancelling', 'Cancelling…'],
    ['SUCCEEDED', 'confirmed', 'Confirmed on the machine'],
    ['FAILED', 'failed', 'Failed:'],
    ['EXPIRED', 'timed-out', "Failed: the machine didn't answer in time. Nothing was changed."],
    ['CANCELED', 'cancelled', 'Cancelled. Nothing was changed.'],
  ])('shows %s as the %s step', async (status, step, line) => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(cmd('c1', status))))
    expect(stepsState({ status })).toBe(step)
    render(<Harness seeds={[{ command: cmd('c1', status, 'UPDATE_USER', status === 'SUCCEEDED' ? { completed_at: '2026-11-20T05:44:00Z' } : { error_code: 'ZKT_USER_WRITE_FAILED' }), name: 'Ayesha Khan' }]} />)
    fireEvent.click(screen.getByText('start'))
    expect(await screen.findByText('Saving changes to Ayesha Khan')).toBeTruthy()
    expect(document.querySelector('.people-change-line')?.textContent).toContain(line)
  })

  it('follows two changes started back to back until both are confirmed', async () => {
    const answers = new Map([['a', 0], ['b', 0]])
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const id = String(input).split('/').pop() || ''
      const count = (answers.get(id) || 0) + 1
      answers.set(id, count)
      return respond(cmd(id, count >= 2 ? 'SUCCEEDED' : 'RUNNING', id === 'a' ? 'CREATE_USER' : 'DELETE_USER'))
    }))
    const finals: string[] = []
    render(<Harness seeds={[{ command: cmd('a', 'QUEUED', 'CREATE_USER'), name: 'Ayesha Khan' }, { command: cmd('b', 'QUEUED', 'DELETE_USER'), name: 'Bilal Ahmed' }]} onFinal={(command) => finals.push(command.command_id)} />)
    fireEvent.click(screen.getByText('start'))
    expect(screen.getByText('Adding Ayesha Khan')).toBeTruthy()
    expect(screen.getByText('Removing Bilal Ahmed')).toBeTruthy()
    await waitFor(() => expect(finals.sort()).toEqual(['a', 'b']), { timeout: 8000 })
    expect(screen.getByTestId('steps').textContent).toBe('Ayesha Khan:confirmed,Bilal Ahmed:confirmed')
  }, 12_000)

  it('stops asking about a CANCELED change', async () => {
    const fetchMock = vi.fn(async () => respond(cmd('c', 'CANCELED')))
    vi.stubGlobal('fetch', fetchMock)
    render(<Harness seeds={[{ command: cmd('c', 'QUEUED'), name: 'Ayesha Khan' }]} />)
    fireEvent.click(screen.getByText('start'))
    await waitFor(() => expect(screen.getByText('Cancelled. Nothing was changed.')).toBeTruthy())
    const calls = fetchMock.mock.calls.length
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 2500)) })
    expect(fetchMock.mock.calls.length).toBe(calls)
  }, 10_000)

  it('offers Try again on a failed change', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(cmd('f', 'FAILED', 'CREATE_USER', { error_code: 'ZKT_USER_CREATE_FAILED' }))))
    const onRetry = vi.fn()
    render(<Harness seeds={[{ command: cmd('f', 'FAILED', 'CREATE_USER', { error_code: 'ZKT_USER_CREATE_FAILED' }), name: 'Ayesha Khan', retry: true }]} onRetry={onRetry} />)
    fireEvent.click(screen.getByText('start'))
    expect((await screen.findAllByText(/Failed: The machine didn't add the person/)).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(onRetry).toHaveBeenCalled()
  })
})
