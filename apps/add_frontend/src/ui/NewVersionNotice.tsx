import { useState } from 'react'

// Shown above the page title when the server has a newer dashboard. It never
// reloads by itself: a form may be half filled.
export function NewVersionNotice({ available, onReload = () => window.location.reload() }: { available: boolean; onReload?: () => void }) {
  const [busy, setBusy] = useState(false)
  if (!available) return null
  return (
    <div className="new-version-notice" role="status">
      <span>A new version of the dashboard is ready.</span>
      <button type="button" className="button secondary small" disabled={busy} aria-busy={busy || undefined} onClick={() => { setBusy(true); onReload() }}>
        {busy ? 'Reloading…' : 'Reload'}
      </button>
    </div>
  )
}
