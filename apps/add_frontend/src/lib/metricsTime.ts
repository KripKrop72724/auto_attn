import { clock } from './time'

/**
 * Pakistan time ("2:15 pm", no seconds) for when the server computed cached
 * dashboard figures (`metrics_computed_at` on the overview and the recovery
 * summary), or null when the backend did not send a usable value.
 */
export function formatMetricsTime(computedAt: unknown): string | null {
  if (typeof computedAt !== 'string' || !computedAt) return null
  return clock(computedAt) || null
}
