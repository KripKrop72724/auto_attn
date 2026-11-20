// Settings (`/settings`, 01-foundation K, FND-13): show technical details,
// check-in phone messages (moved here from Attendance; the settings dialog and
// its password step stay exactly as they are), contact IT, the old screens
// fallback and the account. No Division switch on this page.
import { useState } from 'react'
import { useAppData } from '../AppData'
import { Icon } from '../Icon'
import { usePrefs } from '../lib/prefs'
import { PageHeader } from '../ui/PageHeader'
import { FRAME_WORDS, SETTINGS_WORDS } from '../words/home'
import { CheckinNotifySettingsCard } from './CheckinNotification'
import './Settings.css'

export const CLASSIC_PATH = '/classic/'

export function SettingsView() {
  const app = useAppData()
  const [prefs, setPrefs] = usePrefs()
  const [signingOut, setSigningOut] = useState(false)

  return (
    <div className="settings-page">
      <PageHeader title={SETTINGS_WORDS.title} description={SETTINGS_WORDS.description} />

      <section className="panel settings-section" aria-labelledby="settings-display">
        <h2 id="settings-display">{SETTINGS_WORDS.display}</h2>
        <label className="settings-switch">
          <input type="checkbox" role="switch" checked={prefs.technical} onChange={(event) => setPrefs({ technical: event.target.checked })} />
          <span>
            <strong>{SETTINGS_WORDS.technical}</strong>
            <small>{SETTINGS_WORDS.technicalHelp}</small>
          </span>
        </label>
      </section>

      {/* The Attendance page's phone-message card moved here (ATT, FND-13); its
          settings dialog keeps its reason and password step exactly as before. */}
      <CheckinNotifySettingsCard toast={app.toast} devices={app.devices} />

      <section className="panel settings-section" aria-labelledby="settings-contact">
        <h2 id="settings-contact">{SETTINGS_WORDS.contact}</h2>
        <p className="settings-help">{SETTINGS_WORDS.contactBody}</p>
      </section>

      <section className="panel settings-section" aria-labelledby="settings-classic">
        <h2 id="settings-classic">{SETTINGS_WORDS.oldScreens}</h2>
        <p className="settings-help">{SETTINGS_WORDS.oldScreensBody}</p>
        <a className="button secondary settings-classic-link" href={CLASSIC_PATH}>
          {SETTINGS_WORDS.oldScreensLink} <Icon name="external" size="sm" />
        </a>
      </section>

      <section className="panel settings-section settings-account" aria-labelledby="settings-account">
        <h2 id="settings-account">{SETTINGS_WORDS.account}</h2>
        <div className="settings-account-row">
          <p>{FRAME_WORDS.signedInAs(app.username)}</p>
          <button
            type="button"
            className="button secondary"
            disabled={signingOut}
            aria-busy={signingOut || undefined}
            onClick={() => { setSigningOut(true); void app.logout().finally(() => setSigningOut(false)) }}
          >
            <Icon name="logout" size="sm" /> {signingOut ? FRAME_WORDS.signingOut : FRAME_WORDS.signOut}
          </button>
        </div>
      </section>
    </div>
  )
}
