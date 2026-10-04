import { describe, expect, it } from 'vitest'
import type { CaptureLatencyHistogram } from '../types'
import { captureLatencyLabel } from './captureLatency'

const measured: CaptureLatencyHistogram = { schema_version: 1, samples: 100, max_ms: 17000,
  buckets: [0, 0, 0, 95, 0, 0, 0, 0, 4, 0, 0, 0, 1], saturated: false }

describe('capture timing bounds', () => {
  it('reports a conservative percentile and its denominator', () => {
    expect(captureLatencyLabel(measured)).toBe('p99 ≤ 500 ms · 100 complete samples')
    expect(captureLatencyLabel({ ...measured, buckets: [0, 0, 0, 95, 0, 0, 0, 0, 3, 0, 0, 0, 2] }))
      .toBe('p99 ≤ 17,000 ms · 100 complete samples')
  })
  it('keeps empty, saturated and malformed data unknown', () => {
    expect(captureLatencyLabel()).toBe('Not reported')
    expect(captureLatencyLabel({ ...measured, samples: 0, max_ms: 0, buckets: Array(13).fill(0) })).toBe('No complete samples')
    expect(captureLatencyLabel({ ...measured, samples: 0xffffffff, saturated: true, max_ms: 0,
      buckets: [0xffffffff, ...Array(12).fill(0)] })).toBe('Counter exhausted; percentile unavailable')
    for (const change of [{ samples: 99 }, { max_ms: 500 }, { saturated: true }, { buckets: [1] }]) {
      expect(captureLatencyLabel({ ...measured, ...change })).toBe('Not reported')
    }
  })
})
