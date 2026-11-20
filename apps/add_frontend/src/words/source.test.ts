// WORDS-12: codes are turned into words only inside src/words/. A page that
// reads a code by swapping its underscores for spaces ("WAITING_RESOURCE" →
// "Waiting resource") shows people a system word in disguise, so the old
// translators may not spread. Engineer text for Technical details uses
// words/technical.ts.
//
// PENDING lists the old screens that still call them in this release, each
// with its reason. The list may only shrink: a new file fails the test, and a
// listed file that no longer needs its entry fails too.
import { describe, expect, it } from 'vitest'

const sources = import.meta.glob(['../**/*.{ts,tsx}', '!../**/*.test.{ts,tsx}', '!../test/**', '!../words/**'], {
  query: '?raw', import: 'default', eager: true,
}) as Record<string, string>

const PATTERNS: ReadonlyArray<{ name: string; pattern: RegExp }> = [
  { name: 'humanizeStatus(', pattern: /\bhumanizeStatus\(/ },
  { name: "replaceAll('_'", pattern: /replaceAll\(\s*['"]_['"]/ },
  { name: '.replace(/_/g', pattern: /\.replace\(\s*\/_\/g/ },
]

const PENDING: Record<string, { uses: readonly string[]; why: string }> = {
  '../status.ts': { uses: ['humanizeStatus(', "replaceAll('_'"], why: 'defines humanizeStatus for the screens below' },
  '../ui/StatusBadge.tsx': { uses: ['humanizeStatus('], why: 'old status pill, still used by Set up a device and the old send-task details' },
  '../features/FirmwareProvisioning.tsx': { uses: ["replaceAll('_'"], why: 'Set up a device wizard words wait for ADV-20' },
  '../features/AttendanceRelease.tsx': { uses: ["replaceAll('_'"], why: 'old send-task details under Fix tools › History' },
  '../features/SafeAttendanceRepair.tsx': { uses: ['humanizeStatus('], why: 'retired repair history under Fix tools › History' },
  '../features/ZktCustodyStatus.tsx': { uses: ['humanizeStatus('], why: 'engineer view behind Tools › Advanced tools' },
  '../features/AlertResolveDialog.tsx': { uses: ['humanizeStatus('], why: 'device error re-check dialog, words owned by 04-device' },
}

describe('old code translators stay out of the pages (WORDS-12)', () => {
  it('finds the source files', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50)
  })

  it('no file outside src/words/ reads codes by swapping underscores, except the listed old screens', () => {
    const found: Record<string, string[]> = {}
    for (const [file, text] of Object.entries(sources)) {
      const uses = PATTERNS.filter(({ pattern }) => pattern.test(text)).map(({ name }) => name)
      if (uses.length) found[file] = uses
    }
    const unexpected = Object.entries(found)
      .flatMap(([file, uses]) => uses.filter((use) => !PENDING[file]?.uses.includes(use)).map((use) => `${file}: ${use}`))
    expect(unexpected).toEqual([])
    const stale = Object.entries(PENDING)
      .flatMap(([file, entry]) => entry.uses.filter((use) => !found[file]?.includes(use)).map((use) => `${file}: ${use}`))
    expect(stale).toEqual([])
  })
})
