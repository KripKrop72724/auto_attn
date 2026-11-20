import { useRef, useState, type KeyboardEvent } from 'react'
import { AnchoredLayer } from '../AnchoredLayer'
import { Icon, type IconName } from '../Icon'
import { IconButton } from './Button'
import { UI_TEXT } from './uiText'
import { cx } from './cx'
import './content.css'

export type ActionMenuItem = {
  id: string
  label: string
  icon?: IconName
  href?: string
  /** Runs once on choose; the menu never adds a confirmation of its own. */
  onSelect?: () => void
  danger?: boolean
  disabled?: boolean
}

// The "⋯" button and its menu: everything after the page's one primary action.
export function ActionMenu({ items, label = UI_TEXT.moreActions, className }: { items: ActionMenuItem[]; label?: string; className?: string }) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  if (!items.length) return null
  const focusItem = (step: number | 'first' | 'last') => {
    const entries = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])') || [])
    if (!entries.length) return
    const index = entries.indexOf(document.activeElement as HTMLElement)
    const next = step === 'first' ? 0 : step === 'last' ? entries.length - 1 : (index + step + entries.length) % entries.length
    entries[next]?.focus()
  }
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === 'ArrowDown') { event.preventDefault(); focusItem(1) }
    if (event.key === 'ArrowUp') { event.preventDefault(); focusItem(-1) }
    if (event.key === 'Home') { event.preventDefault(); focusItem('first') }
    if (event.key === 'End') { event.preventDefault(); focusItem('last') }
    if (event.key === 'Tab') setOpen(false)
  }
  const close = (refocus: boolean) => {
    setOpen(false)
    if (refocus) triggerRef.current?.focus()
  }
  return (
    <>
      <IconButton
        ref={triggerRef}
        icon="more"
        label={label}
        className={cx('ui-action-menu-trigger', className)}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          setOpen((value) => !value)
          window.requestAnimationFrame(() => focusItem('first'))
        }}
      />
      {open && (
        <AnchoredLayer anchorRef={triggerRef} preferredWidth={240} onDismiss={(reason) => close(reason === 'escape')}>
          <div ref={menuRef} className="ui-menu" role="menu" aria-label={label} onKeyDown={onKeyDown}>
            {items.map((item) => item.href && !item.disabled
              ? <a key={item.id} role="menuitem" tabIndex={-1} className={cx('ui-menu-item', item.danger && 'is-danger')} href={item.href} onClick={() => { item.onSelect?.(); close(false) }}>{item.icon && <Icon name={item.icon} size="sm" />}<span>{item.label}</span></a>
              : <button key={item.id} type="button" role="menuitem" tabIndex={-1} className={cx('ui-menu-item', item.danger && 'is-danger')} aria-disabled={item.disabled || undefined} onClick={() => { if (item.disabled) return; close(true); item.onSelect?.() }}>{item.icon && <Icon name={item.icon} size="sm" />}<span>{item.label}</span></button>)}
          </div>
        </AnchoredLayer>
      )}
    </>
  )
}
