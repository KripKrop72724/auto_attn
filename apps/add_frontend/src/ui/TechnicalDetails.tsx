import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { Icon } from '../Icon'
import { UI_TEXT } from './uiText'
import { cx } from './cx'

// The "Show technical details" preference. The preferences module (FND-8)
// provides the value; without a provider every box starts closed.
const TechnicalDetailsContext = createContext(false)

export function TechnicalDetailsProvider({ show, children }: { show: boolean; children: ReactNode }) {
  return <TechnicalDetailsContext.Provider value={show}>{children}</TechnicalDetailsContext.Provider>
}

export const useShowTechnical = () => useContext(TechnicalDetailsContext)

export function Disclosure({ summary, children, defaultOpen = false, className, technical = false }: { summary: ReactNode; children: ReactNode; defaultOpen?: boolean; className?: string; technical?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  useEffect(() => setOpen(defaultOpen), [defaultOpen])
  return (
    <details
      className={cx('ui-disclosure', className)}
      open={open}
      data-technical={technical ? '' : undefined}
      onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary><Icon name="chevron" size="sm" className="ui-disclosure-chevron" /><span>{summary}</span></summary>
      <div className="ui-disclosure-body">{children}</div>
    </details>
  )
}

// Codes, MAC, IP, serial, ids, raw messages: never deleted, kept here, closed
// by default and opened when the person turned on "Show technical details".
// The plain-words guard skips everything inside [data-technical].
export function TechnicalDetails({ children, summary = UI_TEXT.technicalDetails, copyText, defaultOpen, className }: { children: ReactNode; summary?: ReactNode; copyText?: string; defaultOpen?: boolean; className?: string }) {
  const show = useShowTechnical()
  const bodyRef = useRef<HTMLDivElement>(null)
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), 1500)
    return () => window.clearTimeout(timer)
  }, [copied])
  const copy = async () => {
    const text = copyText ?? bodyRef.current?.innerText ?? bodyRef.current?.textContent ?? ''
    try {
      await navigator.clipboard?.writeText(text)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }
  return (
    <Disclosure summary={summary} defaultOpen={defaultOpen ?? show} className={cx('ui-technical', className)} technical>
      <div className="ui-technical-body" ref={bodyRef}>{children}</div>
      {typeof navigator !== 'undefined' && navigator.clipboard && (
        <button type="button" className="text-button ui-technical-copy" onClick={() => void copy()} aria-live="polite">
          <Icon name={copied ? 'check' : 'copy'} size="sm" />
          <span>{copied ? UI_TEXT.copied : UI_TEXT.copy}</span>
        </button>
      )}
    </Disclosure>
  )
}
