import { usePrefs } from '../lib/prefs'
import { PageHeader } from '../ui/PageHeader'

// Settings (`/settings`). Placeholder: the Settings page (01-foundation §K,
// FND-13) fills this module with the check-in phone messages and the account.
export default function SettingsRoute() {
  const [prefs, setPrefs] = usePrefs()
  return (
    <>
      <PageHeader title="Settings" description="Choices for how the dashboard shows things to you." />
      <section className="panel settings-section" aria-labelledby="settings-display">
        <header className="panel-header"><div><h2 id="settings-display">Display</h2></div></header>
        <label className="settings-switch">
          <input type="checkbox" checked={prefs.technical} onChange={(event) => setPrefs({ technical: event.target.checked })} />
          <span>
            <strong>Show technical details</strong>
            <small>Opens the "Technical details" parts of every page for IT. Saved in this browser.</small>
          </span>
        </label>
      </section>
    </>
  )
}
