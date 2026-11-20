import type { CaptureLatencyHistogram } from '../types'

const bounds = [0, 1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000, 15000, 0xffffffff]
const counter = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v) && v >= 0 && v <= 0xffffffff

// Inclusive, non-cumulative buckets describe successful operations only.
// A boot/capture restart starts a different series; never average percentiles.
export function captureLatencyLabel(value?: CaptureLatencyHistogram | null): string {
  if (!value || value.schema_version !== 1 || !counter(value.samples) || !counter(value.max_ms) ||
    typeof value.saturated !== 'boolean' || !Array.isArray(value.buckets) || value.buckets.length !== bounds.length ||
    !value.buckets.every(counter) || value.buckets.reduce((a, b) => a + b, 0) !== value.samples) return 'Not reported'
  if (!value.samples) return value.max_ms === 0 && !value.saturated ? 'No complete samples' : 'Not reported'
  let last = bounds.length - 1
  while (!value.buckets[last]) last--
  if (bounds.findIndex(upper => value.max_ms <= upper) !== last) return 'Not reported'
  if (value.saturated) return value.samples === 0xffffffff ? 'Counter exhausted; percentile unavailable' : 'Not reported'
  const rank = Math.ceil(value.samples * 99 / 100)
  let total = 0
  for (let i = 0; i < bounds.length; i++) {
    total += value.buckets[i]
    if (total >= rank) return `p99 ≤ ${Math.min(bounds[i], value.max_ms).toLocaleString()} ms · ${value.samples.toLocaleString()} complete samples`
  }
  return 'Not reported'
}
