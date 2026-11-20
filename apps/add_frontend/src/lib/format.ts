// Numbers always use Pakistan grouping ("158,560"), never the browser's locale.
const numberFormat = new Intl.NumberFormat('en-PK', { maximumFractionDigits: 0 })
const decimalFormat = new Intl.NumberFormat('en-PK', { maximumFractionDigits: 1 })

/** "158,560". A missing or broken value is an empty string, never "0" or "NaN". */
export function formatNumber(value: number | null | undefined, { decimals = false } = {}): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return ''
  return (decimals ? decimalFormat : numberFormat).format(value)
}

/** "1 device", "3 devices": the count with thousands separators and the right word. */
export function plural(count: number, one: string, many = `${one}s`): string {
  return `${formatNumber(count)} ${count === 1 ? one : many}`
}

/** "60%", rounded. Empty when the total is zero or missing. */
export function formatPercent(part: number | null | undefined, total: number | null | undefined): string {
  if (typeof part !== 'number' || typeof total !== 'number' || !total) return ''
  return `${Math.round((part / total) * 100)}%`
}
