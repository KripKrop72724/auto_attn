// The account button and its menu on wider screens (01-foundation D): who is
// signed in, "Show technical details", Settings, the public status page and
// Sign out ("Signing out…" while it runs).
import { useEffect, useRef, useState, type MouseEvent } from 'react'
import { Icon } from '../Icon'
import { usePrefs } from '../lib/prefs'
import { useDismiss } from '../ui/useDismiss'
import { FRAME_WORDS } from '../words/home'

export function operatorInitials(name: string) {
  const words = name.replace(/([a-z])([A-Z])/g, '$1 $2').split(/[\s._@-]+/).filter(Boolean)
  if (!words.length) return '?'
  const letters = words.length === 1 ? words[0].slice(0, 2) : `${words[0][0]}${words[words.length - 1][0]}`
  return letters.toUpperCase()
}

export function TechnicalSwitch({ id }: { id: string }) {
  const [prefs, setPrefs] = usePrefs()
  return (
    <label className="shell-switch" htmlFor={id}>
      <span>{FRAME_WORDS.showTechnical}</span>
      <input id={id} type="checkbox" role="switch" checked={prefs.technical} onChange={(event) => setPrefs({ technical: event.target.checked })} />
    </label>
  )
}

export function SignOutButton({ onLogout, className = 'button secondary' }: { onLogout: () => Promise<void>; className?: string }) {
  const [busy, setBusy] = useState(false)
  return (
    <button type="button" className={className} disabled={busy} aria-busy={busy || undefined} onClick={() => { setBusy(true); void onLogout().finally(() => setBusy(false)) }}>
      <Icon name="logout" size="sm" /> {busy ? FRAME_WORDS.signingOut : FRAME_WORDS.signOut}
    </button>
  )
}

export function AccountMenu({ username, onLogout, onSettings }: {
  username: string
  onLogout: () => Promise<void>
  onSettings: (event: MouseEvent<HTMLAnchorElement>) => void
}) {
  const [open, setOpen] = useState(false)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  useDismiss(() => { setOpen(false); buttonRef.current?.focus() }, { enabled: open })
  useEffect(() => {
    if (!open) return
    menuRef.current?.querySelector<HTMLElement>('input, a, button')?.focus()
    const close = (event: PointerEvent) => {
      const target = event.target as Node
      if (menuRef.current?.contains(target) || buttonRef.current?.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [open])
  return (
    <span className="account-menu">
      <button
        ref={buttonRef}
        type="button"
        className="account-button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={FRAME_WORDS.account(username)}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="operator-avatar" aria-hidden="true" data-user-text>{operatorInitials(username)}</span>
        <span className="account-name" data-user-text>{username}</span>
        <Icon name="chevronDown" size="sm" />
      </button>
      {open && (
        <div ref={menuRef} className="account-panel" role="menu" aria-label={FRAME_WORDS.account(username)}>
          <p className="account-who">{FRAME_WORDS.signedInAs(username)}</p>
          <div role="none" className="account-row"><TechnicalSwitch id="account-technical" /></div>
          <a role="menuitem" className="account-link" href="/settings" onClick={(event) => { setOpen(false); onSettings(event) }}>
            <Icon name="settings" size="sm" /> {FRAME_WORDS.settings}
          </a>
          <a role="menuitem" className="account-link" href="/status" target="_blank" rel="noopener">
            <Icon name="external" size="sm" /> {FRAME_WORDS.publicStatus} <span className="sr-only">{FRAME_WORDS.opensInNewTab}</span>
          </a>
          <div role="none" className="account-row account-sign-out"><SignOutButton onLogout={onLogout} className="text-button account-link" /></div>
        </div>
      )}
    </span>
  )
}
