// Per-device People counts for the picker (GET /api/v2/people/summary, PPL-2).
// The counts are a convenience: when the request fails the picker just hides
// them, it never shows 0 and never shows an error.
import { useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import type { PeopleSummary, PeopleSummaryRow } from './model'

export function usePeopleSummary(revision = 0): { rows: Map<string, PeopleSummaryRow> | null; failed: boolean } {
  const [rows, setRows] = useState<Map<string, PeopleSummaryRow> | null>(null)
  const [failed, setFailed] = useState(false)
  const held = useRef(false)
  useEffect(() => {
    const controller = new AbortController()
    api<PeopleSummary>('/api/v2/people/summary', { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return
        if (!value || !Array.isArray(value.rows)) {
          if (!held.current) setFailed(true)
          return
        }
        held.current = true
        setRows(new Map(value.rows.map((row) => [row.connector_id, row])))
        setFailed(false)
      })
      .catch(() => {
        if (!controller.signal.aborted && !held.current) setFailed(true)
      })
    return () => controller.abort()
  }, [revision])
  return { rows, failed }
}
