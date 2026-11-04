/**
 * Local "hh:mm:ss" for the time the server computed cached dashboard figures
 * (`metrics_computed_at` on the overview and the recovery summary), or null
 * when the backend did not send a usable value.
 */
export function formatMetricsTime(computedAt: unknown): string | null {
  if (typeof computedAt !== 'string' || !computedAt) return null
  const at = new Date(computedAt)
  if (Number.isNaN(at.getTime())) return null
  return at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
