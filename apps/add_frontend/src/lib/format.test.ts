import { describe, expect, it } from 'vitest'
import { formatNumber, formatPercent, plural } from './format'

describe('number formatting', () => {
  it('groups thousands the Pakistan way and never prints NaN or a fake zero', () => {
    expect(formatNumber(158560)).toBe('158,560')
    expect(formatNumber(0)).toBe('0')
    expect(formatNumber(12.6)).toBe('13')
    expect(formatNumber(12.65, { decimals: true })).toBe('12.7')
    expect(formatNumber(Number.NaN)).toBe('')
    expect(formatNumber(null)).toBe('')
    expect(formatNumber(undefined)).toBe('')
  })

  it('says one or many with the count', () => {
    expect(plural(1, 'device')).toBe('1 device')
    expect(plural(2, 'device')).toBe('2 devices')
    expect(plural(1200, 'person', 'people')).toBe('1,200 people')
  })

  it('rounds percentages and stays empty without a total', () => {
    expect(formatPercent(3, 5)).toBe('60%')
    expect(formatPercent(3, 0)).toBe('')
    expect(formatPercent(null, 5)).toBe('')
  })
})
