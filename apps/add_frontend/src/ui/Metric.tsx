import { Icon } from '../Icon'
import { formatNumber } from '../lib/format'
import { formatMetricsTime } from '../lib/metricsTime'
import type { Overview } from '../types'

export type MetricTone = 'neutral' | 'positive' | 'warning' | 'critical'

// `loading`: the figure has not arrived yet. It shows "Loading…", never a 0.
export function Metric({ label, value, detail, icon, tone = 'neutral', onClick, loading = false }: { label: string; value: string | number; detail: string; icon: Parameters<typeof Icon>[0]['name']; tone?: MetricTone; onClick?: () => void; loading?: boolean }) {
  const shown = loading ? '…' : typeof value === 'number' ? formatNumber(value) : value
  const content = <div>
      <p><Icon name={icon} />{label}</p>
      <strong aria-hidden={loading || undefined}>{shown}</strong>
      <small>{loading ? 'Loading…' : detail}</small>
    </div>
  if (loading) return <article className={`metric-card metric-neutral is-loading`} aria-busy="true" aria-label={`${label}: loading`}>{content}</article>
  return onClick
    ? <button type="button" className={`metric-card metric-${tone} metric-link`} onClick={onClick} aria-label={`${label}: ${shown}. ${detail}`}>{content}</button>
    : <article className={`metric-card metric-${tone}`}>{content}</article>
}

// The server may serve cached overview metrics; say how old they are. Older
// backends omit the field and nothing is shown.
export function metricsTime(overview: Overview) {
  return formatMetricsTime(overview.metrics_computed_at)
}

// Availability is only calm when nearly every connector is online.
export const availabilityTone = (percent: number, total: number): MetricTone =>
  !total ? 'neutral' : percent >= 95 ? 'positive' : percent >= 80 ? 'warning' : 'critical'
