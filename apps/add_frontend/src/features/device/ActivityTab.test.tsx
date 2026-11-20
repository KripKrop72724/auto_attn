import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DeviceLog } from '../../types'
import { expectPlainText } from '../../words/testing'
import { activityWindow, keptDays } from './activityData'
import { calls, device, json, renderDevice, resetDeviceTests, stubFetch, type Handler } from './testHarness'

const now = Date.now()
const at = (minutesAgo: number) => new Date(now - minutesAgo * 60_000).toISOString()

const raw = (overrides: Partial<DeviceLog>): DeviceLog => ({
  id: 18234,
  boot_id: 'ac276ea4-0000-4000-8000-000000000001',
  sequence: 1,
  level: 'ERROR',
  subsystem: 'zkt',
  code: 'ZKT_TIME_SYNC_READBACK_FAILED',
  message: 'ZKT clock update did not verify within five seconds of Pakistan Standard Time.',
  context: { drift_seconds: 7 },
  device_time: at(5),
  received_at: at(5),
  ...overrides,
})

const page = {
  items: [
    { type: 'event', id: 18240, at: at(2), kind: 'problem', serious: false, topic: 'storage', title: 'Problem started: Link box cannot save attendance safely.', action: 'Tell IT.', link: null, fixed_at: null, code: 'STORAGE_FILE_UNREADABLE', raw: raw({ id: 18240, level: 'WARN', subsystem: 'storage', code: 'STORAGE_FILE_UNREADABLE', message: 'file /spiffs/live.q unreadable errno=5', received_at: at(2) }) },
    { type: 'event', id: 18234, at: at(5), kind: 'retried', serious: false, topic: 'machine', title: "Set the machine's clock but couldn't confirm it. Trying again in a minute.", action: null, link: null, fixed_at: at(4), code: 'ZKT_TIME_SYNC_READBACK_FAILED', raw: raw({}) },
    { type: 'group', kind: 'routine', topic: 'machine', code: 'ZKT_TIME_SYNC_OK', count: 3, first_at: at(60), last_at: at(10), first_id: 18100, last_id: 18102, title: "Set the machine's clock to Pakistan time 3 times. All fine.", action: null, fixed_at: null },
    { type: 'action', at: at(70), command_id: 'cmd-1', command_type: 'REFRESH_USERS', status: 'SUCCEEDED', actor: 'StateHealthAdmin', title: 'StateHealthAdmin refreshed the people list. Confirmed.', topic: 'people', kind: 'action' },
  ],
  next_cursor: '2026-11-20T00:00:00Z|18000',
  newest_id: 18240,
  counts: { problems: 1, retried: 1, routine: 3, by_topic: { machine: 1, link_box: 0, hr_system: 0, people: 0, software: 0, storage: 1 } },
  window: { since: at(1440), until: at(0) },
  retention_days: 14,
}

let activity: Handler = (path) => (path.includes('/activity?') ? json(page) : undefined)
const rest: Handler = (path) => {
  if (path.includes('/alerts?')) return json({ rows: [] })
  if (path.includes('/connectivity?')) return json({ rows: [] })
  if (path.endsWith(`/devices/${device.connector_id}`)) return json(device)
  return undefined
}

beforeEach(() => {
  resetDeviceTests()
  activity = (path) => (path.includes('/activity?') ? json(page) : undefined)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const openActivity = (search = '?tab=activity') => {
  stubFetch((path, init) => activity(path, init), rest)
  return renderDevice(device, { search })
}

describe('Activity tab', () => {
  it('shows plain sentences by day, with the code only in the event sheet', async () => {
    openActivity()
    const region = await screen.findByRole('region', { name: 'Device activity' })
    expect(await within(region).findByText('Problem started: Link box cannot save attendance safely.')).toBeTruthy()
    expect(within(region).getByText('Today: 1 problem. 1 thing was retried automatically.')).toBeTruthy()
    expect(within(region).getByRole('heading', { name: /^Today · / })).toBeTruthy()
    expect(within(region).getByText(/^Fixed at /)).toBeTruthy()
    expect(within(region).queryByText(/ZKT_TIME_SYNC_READBACK_FAILED/)).toBeNull()
    expectPlainText(region)

    fireEvent.click(within(region).getByText("Set the machine's clock but couldn't confirm it. Trying again in a minute."))
    const sheet = await screen.findByRole('dialog', { name: 'Trying again' })
    const technical = within(sheet).getByText('Technical details').closest('details') as HTMLDetailsElement
    expect(technical.open).toBe(true)
    expect(within(sheet).getByText('ZKT_TIME_SYNC_READBACK_FAILED')).toBeTruthy()
    expect(within(sheet).getByText('#18234')).toBeTruthy()
    expect(within(sheet).getByRole('button', { name: 'Copy details' })).toBeTruthy()
  })

  it('never marks a row already shown as new on a live update', async () => {
    const newer = { type: 'event', id: 18250, at: at(1), kind: 'routine', serious: false, topic: 'machine', title: 'A brand new line.', action: null, link: null, fixed_at: null, code: 'ZKT_TIME_SYNC_OK', raw: raw({ id: 18250, level: 'INFO', code: 'ZKT_TIME_SYNC_OK', received_at: at(1) }) }
    activity = (path) => {
      if (!path.includes('/activity?')) return undefined
      if (path.includes('after=')) return json({ ...page, items: [newer, page.items[3]], next_cursor: null, newest_id: 18250 })
      return json(page)
    }
    const view = openActivity()
    const region = await screen.findByRole('region', { name: 'Device activity' })
    await within(region).findByText('StateHealthAdmin refreshed the people list. Confirmed.')
    view.rerenderRevision(1)
    const fresh = await within(region).findByText('A brand new line.')
    expect(fresh.closest('li')?.className).toContain('is-new')
    expect(within(region).getAllByText('StateHealthAdmin refreshed the people list. Confirmed.')).toHaveLength(1)
    expect(within(region).getByText('StateHealthAdmin refreshed the people list. Confirmed.').closest('li')?.className).not.toContain('is-new')
  })

  it('asks the server for today in Pakistan time, problems only and a group', async () => {
    openActivity()
    await screen.findByText('Problem started: Link box cannot save attendance safely.')
    const today = activityWindow('today', '')
    expect(calls('/activity?').at(-1)?.[0]).toContain(`since=${encodeURIComponent(today.since)}`)
    expect(String(calls('/activity?').at(-1)?.[0])).toContain(`until=${encodeURIComponent(today.until)}`)
    fireEvent.click(screen.getByRole('button', { name: /^Problems only/ }))
    await waitFor(() => expect(String(calls('/activity?').at(-1)?.[0])).toContain('show=problems'))
    fireEvent.click(screen.getByRole('button', { name: /^HR system/ }))
    await waitFor(() => expect(String(calls('/activity?').at(-1)?.[0])).toContain('topic=hr_system'))
  })

  it('reads a picked day as its Pakistan midnight bounds', async () => {
    openActivity()
    await screen.findByText('Problem started: Link box cannot save attendance safely.')
    const pick = keptDays()[3]
    fireEvent.change(screen.getByLabelText('Day'), { target: { value: pick } })
    const bounds = activityWindow('day', pick)
    await waitFor(() => expect(String(calls('/activity?').at(-1)?.[0])).toContain(`since=${encodeURIComponent(bounds.since)}`))
    expect(bounds.since).toBe(new Date(`${pick}T00:00:00+05:00`).toISOString())
  })

  it('loads older activity with the cursor and says when everything is shown', async () => {
    openActivity()
    fireEvent.click(await screen.findByRole('button', { name: 'Load older activity' }))
    activity = (path) => (path.includes('/activity?') ? json({ ...page, items: [], next_cursor: null }) : undefined)
    await waitFor(() => expect(String(calls('/activity?').at(-1)?.[0])).toContain(`before=${encodeURIComponent(page.next_cursor)}`))
  })

  it('expands a folded run from the stored rows', async () => {
    stubFetch((path) => (path.includes('/logs?') ? json({ rows: [raw({ id: 18101, level: 'INFO', code: 'ZKT_TIME_SYNC_OK', message: 'ZKT terminal time set', received_at: at(30) })], next_cursor: null }) : undefined), (path, init) => activity(path, init), rest)
    renderDevice(device, { search: '?tab=activity' })
    const row = (await screen.findByText("Set the machine's clock to Pakistan time 3 times. All fine.")).closest('li') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: 'Show' }))
    expect(await within(row).findByText("Set the machine's clock to Pakistan time.")).toBeTruthy()
    expect(String(calls('/logs?').at(-1)?.[0])).toContain('code=ZKT_TIME_SYNC_OK')
  })

  it('says nothing was recorded, nothing matched, or that it could not load', async () => {
    activity = (path) => (path.includes('/activity?') ? json({ ...page, items: [], next_cursor: null, counts: { problems: 0, retried: 0, routine: 0, by_topic: {} } }) : undefined)
    openActivity()
    expect(await screen.findByText('Nothing recorded yet. The link box reports here once it is connected.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /^Problems only/ }))
    expect(await screen.findByText('No problems today.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    cleanup()
    resetDeviceTests()
    activity = (path) => (path.includes('/activity?') ? json({ detail: 'boom' }, 500) : undefined)
    openActivity()
    expect(await screen.findByText("Couldn't load activity.")).toBeTruthy()
  })

  it('falls back to the raw rows and the client words on an older backend', async () => {
    activity = (path) => {
      if (path.includes('/activity?')) return json({ detail: 'Not Found' }, 404)
      if (path.includes('/logs?')) return json({ rows: [raw({ id: 9, level: 'INFO', code: 'ZKT_TIME_SYNC_OK', message: 'ZKT terminal time set', received_at: at(3) })], next_cursor: null })
      return undefined
    }
    openActivity()
    expect(await screen.findByText('Showing a simpler view.')).toBeTruthy()
    expect(screen.getByText("Set the machine's clock to Pakistan time.")).toBeTruthy()
  })

  it('shows the technical log with level filters', async () => {
    stubFetch((path) => (path.includes('/logs?') ? json({ rows: [raw({})], next_cursor: 17000 }) : undefined), (path, init) => activity(path, init), rest)
    renderDevice(device, { search: '?tab=activity&view=technical' })
    const region = await screen.findByRole('region', { name: 'Technical log' })
    expect(await within(region).findByText(/\[ZKT_TIME_SYNC_READBACK_FAILED\]/)).toBeTruthy()
    fireEvent.change(within(region).getByLabelText('Level'), { target: { value: 'error' } })
    await waitFor(() => expect(String(calls('/logs?').at(-1)?.[0])).toContain('level=ERROR&level=CRITICAL'))
    fireEvent.click(await within(region).findByRole('button', { name: 'Load older activity' }))
    await waitFor(() => expect(String(calls('/logs?').at(-1)?.[0])).toContain('cursor=17000'))
  })
})
