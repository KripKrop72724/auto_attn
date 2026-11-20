import { afterEach, describe, expect, it } from 'vitest'
import { readUrlValues, writeUrlValues } from '../../lib/urlState'
import { msUntilNextPktMidnight, pktPresetRange } from '../../lib/pktDates'
import { FILTER_SCHEMA, listParams, readView, rewriteLegacySearch, summaryParams, type FilterValues } from './useAttendanceFilters'

const base = readUrlValues(FILTER_SCHEMA, '') as FilterValues
// Node's TZ (no @types/node in this project): changing it changes the zone for new Dates.
const env = (globalThis as unknown as { process: { env: Record<string, string | undefined> } }).process.env
const originalTz = env.TZ

describe('attendance filters in the address', () => {
  afterEach(() => { env.TZ = originalTz })

  it('round-trips every filter through the URL and keeps other keys', () => {
    const filters: FilterValues = {
      ...base, when: 'custom', from: '2026-11-01', to: '2026-11-04', device: ['a', 'b'], status: ['not_matched', 'waiting'],
      type: 'in', by_admin: true, phone: ['failed'], time: 'wrong', arrived: 'later', no_id: true, machine_match: true, event: 12,
    }
    const search = writeUrlValues(FILTER_SCHEMA, '?division=life&force_run=x', filters)
    expect(search).not.toMatch(/cnic|password|reason|confirmation/i)
    expect(readUrlValues(FILTER_SCHEMA, search)).toEqual(filters)
    expect(new URLSearchParams(search).get('division')).toBe('life')
    expect(new URLSearchParams(search).get('force_run')).toBe('x')
    // Defaults are not written; unknown values read as the default.
    expect(writeUrlValues(FILTER_SCHEMA, '', base)).toBe('')
    expect(readUrlValues(FILTER_SCHEMA, '?status=bogus,sent&when=24h').status).toEqual(['sent'])
    expect(readUrlValues(FILTER_SCHEMA, '?when=24h').when).toBe('today')
  })

  it('rewrites old links once and reads the tab and tool', () => {
    expect(rewriteLegacySearch('?view=all-events&direct_run=r')).toBe('?view=checkins&direct_run=r')
    expect(rewriteLegacySearch('?view=needs-review&device_id=c&user_key=u&force_run=f')).toBe('?view=fix-tools&device_id=c&force_run=f&tool=held-staff')
    expect(rewriteLegacySearch('?view=release-history&release_job=j')).toBe('?view=fix-tools&release_job=j&tool=history&kind=old-release')
    expect(rewriteLegacySearch('?view=fix-tools&tool=history')).toBeNull()
    expect(readView('?view=needs-review')).toEqual({ view: 'fix-tools', tool: 'held-staff' })
    expect(readView('?view=fix-tools&tool=nope')).toEqual({ view: 'fix-tools', tool: '' })
    expect(readView('')).toEqual({ view: 'checkins', tool: '' })
  })

  it('sends a 13-digit search as a CNIC and anything else as text', () => {
    const now = Date.parse('2026-11-06T05:00:00Z')
    const cnic = listParams(base, 'all', '35202-1234567-1', now)
    expect(cnic.get('cnic')).toBe('3520212345671')
    expect(cnic.get('q')).toBeNull()
    const short = listParams(base, 'all', '1007', now)
    expect(short.get('q')).toBe('1007')
    expect(short.get('cnic')).toBeNull()
  })

  it('sends Today with only a start, Yesterday and picked dates with an exclusive end', () => {
    const now = Date.parse('2026-11-06T05:00:00Z')
    const today = listParams(base, 'life', '', now)
    expect(today.get('from_time')).toBe('2026-11-05T19:00:00.000Z')
    expect(today.get('to_time')).toBeNull()
    expect(today.get('division')).toBe('life')
    expect(today.get('notify_summary')).toBe('false')
    const yesterday = listParams({ ...base, when: 'yesterday' }, 'all', '', now)
    expect(yesterday.get('from_time')).toBe('2026-11-04T19:00:00.000Z')
    expect(yesterday.get('to_time')).toBe('2026-11-05T19:00:00.000Z')
    expect(yesterday.get('to_exclusive')).toBe('true')
    const picked = summaryParams({ ...base, when: 'custom', from: '2026-11-01', to: '2026-11-04', device: ['a'] }, 'all', now)
    expect(picked.get('from_time')).toBe('2026-10-31T19:00:00.000Z')
    expect(picked.get('to_time')).toBe('2026-11-04T19:00:00.000Z')
    expect(picked.getAll('device_id')).toEqual(['a'])
    expect(pktPresetRange('7d', now).label).toBe('31 Oct – 6 Nov')
    expect(pktPresetRange('custom', now, '2026-10-01', '2026-11-04').days).toBe(35)
  })

  it.each(['UTC', 'America/New_York', 'Asia/Karachi'])('moves Today at Pakistan midnight whatever the browser time zone (%s)', (zone) => {
    env.TZ = zone
    // 23:59 in Pakistan on 6 Nov is 18:59 UTC.
    const beforeMidnight = Date.parse('2026-11-06T18:59:00Z')
    expect(pktPresetRange('today', beforeMidnight).from_time).toBe('2026-11-05T19:00:00.000Z')
    expect(msUntilNextPktMidnight(beforeMidnight)).toBe(60_000)
    const after = beforeMidnight + 60_000
    expect(pktPresetRange('today', after).from_time).toBe('2026-11-06T19:00:00.000Z')
    expect(pktPresetRange('today', after).label).toMatch(/^Today · Sat 7 Nov$/)
  })
})
