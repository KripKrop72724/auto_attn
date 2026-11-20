import { describe, expect, it } from 'vitest'
import { BANNED_WORDS, findPlainWordProblems, isPlainText, plural, pluralWord } from './glossary'

const rules = (text: string, options?: Parameters<typeof findPlainWordProblems>[1]) =>
  findPlainWordProblems(text, options).map((problem) => problem.rule)

describe('findPlainWordProblems', () => {
  it('passes plain sentences', () => {
    for (const text of [
      'Lost the connection to the machine. Trying again.',
      'Sent to HR system (Oracle) at 10:42 am · 2 h ago',
      'Machine password (COMM Key) · Employee ID 518 · CNIC 35202-1234567-1',
      '158,560 check-ins · 60% full · version 2.8.2',
      'Ask IT. Check the Wi-Fi and the USB cable.',
      'Building 9 · Device 3 · Life',
    ]) expect(findPlainWordProblems(text), text).toEqual([])
  })

  it('finds every rule 8 pattern', () => {
    expect(rules('Status ACKED_FIRMWARE')).toContain('snake_upper')
    expect(rules('reason local_write_commit')).toContain('snake_lower')
    expect(rules('Poll OK; reason=ok')).toContain('key_value')
    expect(rules('{"rows": 104}')).toContain('json')
    expect(rules('digest 9f86d081884c7d65')).toContain('hex')
    expect(rules('ac:27:6e:a3:45:44')).toContain('mac')
    expect(rules('at 192.168.18.31')).toContain('ip')
    expect(rules('LED source add_connector.c:3764')).toContain('file_line')
    expect(rules('running zone-lite-2.8.2')).toContain('build_string')
    expect(rules('LF-ZONE-BLD9-03 is offline')).toContain('device_code')
    expect(rules('3 record(s)')).toContain('plural_s')
    expect(rules('Done!')).toContain('exclamation')
    expect(rules('LF only')).toContain('caps')
  })

  it('finds banned system words as whole words, any case', () => {
    expect(rules('ESP heartbeat is stale.')).toEqual(expect.arrayContaining(['banned_word']))
    expect(findPlainWordProblems('Terminal unreachable').map((p) => p.match)).toEqual(['Terminal'])
    expect(rules('Its check-ins are on hold.')).toContain('banned_word')
    expect(rules('Zone Lite logs')).toContain('banned_word')
    expect(rules('Technical details')).toEqual([])
    expect(rules('detailed retail')).toEqual([])
  })

  it('allows Oracle only as "HR system (Oracle)"', () => {
    expect(isPlainText('Sent to HR system (Oracle).')).toBe(true)
    expect(isPlainText('Waiting for Oracle')).toBe(false)
  })

  it('allows brands and ADD only when the caller is inside data-brand', () => {
    expect(rules('Hikvision')).toContain('brand')
    expect(rules('Hikvision', { allowBrand: true })).toEqual([])
    expect(rules('ADD could not process')).toContain('add_brand')
    expect(rules('ADD', { allowAddBrand: true })).toEqual([])
  })

  it('keeps one entry per banned word, in small letters', () => {
    expect(new Set(BANNED_WORDS).size).toBe(BANNED_WORDS.length)
    for (const word of BANNED_WORDS) expect(word).toBe(word.toLowerCase())
    expect(BANNED_WORDS).toEqual(expect.arrayContaining(['held', 'hold', 'kept back', 'terminal', 'oracle']))
  })
})

describe('plural', () => {
  it('uses real plurals with thousands separators, never "(s)"', () => {
    expect(plural(1, 'check-in')).toBe('1 check-in')
    expect(plural(1200, 'check-in')).toBe('1,200 check-ins')
    expect(plural(2, 'person')).toBe('2 people')
    expect(plural(3, 'entry')).toBe('3 entries')
    expect(plural(2, 'failed try', 'failed tries')).toBe('2 failed tries')
    expect(pluralWord(2, 'device')).toBe('devices')
  })
})
