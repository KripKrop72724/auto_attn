import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type MouseEvent,
  type ReactNode,
} from 'react'
import { Icon, type IconName } from '../Icon'
import { Spinner } from './Spinner'
import { cx } from './cx'

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'danger-outline' | 'ghost' | 'link'
export type ButtonSize = 'sm' | 'md' | 'lg'

const variantClass: Record<ButtonVariant, string> = {
  primary: 'button primary',
  secondary: 'button secondary',
  danger: 'button destructive',
  'danger-outline': 'button destructive-outline',
  ghost: 'button ui-button-ghost',
  link: 'text-button ui-button-link',
}
const sizeClass: Record<ButtonSize, string> = { sm: 'small', md: '', lg: 'large' }

export type ButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onClick'> & {
  variant?: ButtonVariant
  size?: ButtonSize
  /** Shows a spinner and busyLabel, disables the button and keeps its width. */
  busy?: boolean
  /** Text while busy, for example "Saving…". Defaults to the normal label. */
  busyLabel?: ReactNode
  /** Shown with a tick for 1.2 s after an onClick promise resolves. */
  doneLabel?: ReactNode
  icon?: IconName
  iconEnd?: IconName
  fullWidth?: boolean
  /** Returning a promise makes the button busy until it settles. Called once per click; never asks "Are you sure?". */
  onClick?: (event: MouseEvent<HTMLButtonElement>) => unknown
}

const isPromise = (value: unknown): value is Promise<unknown> =>
  Boolean(value) && typeof (value as Promise<unknown>).then === 'function'

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({
  variant = 'secondary',
  size = 'md',
  busy = false,
  busyLabel,
  doneLabel,
  icon,
  iconEnd,
  fullWidth = false,
  className,
  children,
  disabled,
  type = 'button',
  onClick,
  ...rest
}, forwardedRef) {
  const ref = useRef<HTMLButtonElement>(null)
  useImperativeHandle(forwardedRef, () => ref.current as HTMLButtonElement)
  const [pending, setPending] = useState(false)
  const [done, setDone] = useState(false)
  const [lockedWidth, setLockedWidth] = useState<number | null>(null)
  const mounted = useRef(true)
  useEffect(() => () => { mounted.current = false }, [])
  const isBusy = busy || pending

  useEffect(() => {
    if (isBusy) {
      const width = ref.current?.offsetWidth || 0
      setLockedWidth(width > 0 ? width : null)
    } else {
      setLockedWidth(null)
    }
  }, [isBusy])

  useEffect(() => {
    if (!done) return
    const timer = window.setTimeout(() => setDone(false), 1200)
    return () => window.clearTimeout(timer)
  }, [done])

  const handleClick = (event: MouseEvent<HTMLButtonElement>) => {
    // Busy stays focusable (aria-disabled), so a keyboard user keeps their place;
    // the click, and a form submit it would start, are ignored meanwhile.
    if (isBusy) {
      event.preventDefault()
      return
    }
    const result = onClick?.(event)
    if (!isPromise(result)) return
    setPending(true)
    result.then(
      () => { if (mounted.current && doneLabel) setDone(true) },
      () => undefined,
    ).finally(() => { if (mounted.current) setPending(false) })
  }

  const content = isBusy
    ? <><Spinner size="sm" /><span>{busyLabel ?? children}</span></>
    : done && doneLabel
      ? <><Icon name="check" size="sm" /><span>{doneLabel}</span></>
      : <>{icon && <Icon name={icon} size="sm" />}{children !== undefined && children !== null && <span>{children}</span>}{iconEnd && <Icon name={iconEnd} size="sm" />}</>

  return (
    <button
      ref={ref}
      type={type}
      className={cx('ui-button', variantClass[variant], sizeClass[size], fullWidth && 'full', isBusy && 'is-busy', className)}
      disabled={disabled}
      aria-disabled={(isBusy && !disabled) || undefined}
      aria-busy={isBusy || undefined}
      style={lockedWidth ? { minWidth: lockedWidth } : undefined}
      onClick={handleClick}
      {...rest}
    >
      {content}
    </button>
  )
})

export type IconButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children' | 'aria-label'> & {
  /** Accessible name, required: there is no visible text. */
  label: string
  icon: IconName
  busy?: boolean
}

// Square icon-only button with a 44 px hit area on touch. No hover-only title:
// the name is spoken, and visible text belongs next to it when it matters.
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton({
  label,
  icon,
  busy = false,
  className,
  type = 'button',
  disabled,
  ...rest
}, ref) {
  return (
    <button
      ref={ref}
      type={type}
      className={cx('icon-button ui-icon-button', className)}
      aria-label={label}
      aria-busy={busy || undefined}
      disabled={disabled || busy}
      {...rest}
    >
      {busy ? <Spinner size="sm" /> : <Icon name={icon} />}
    </button>
  )
})
