// Sign-in (01-foundation F, FND-11): no pre-filled account name, plain error
// sentences, a notice when a session ended, and a link to the public status page.
import { useEffect, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { ApiError } from '../api'
import { Icon } from '../Icon'

export const LOGIN_WORDS = {
  title: 'State Life Attendance',
  intro: "Sign in to look after State Life's attendance machines.",
  sessionEnded: 'You were signed out. Sign in again to continue where you left off.',
  username: 'Username',
  password: 'Password',
  show: 'Show',
  hide: 'Hide',
  showPassword: 'Show password',
  signIn: 'Sign in',
  signingIn: 'Signing in…',
  needUsername: 'Enter your username.',
  needPassword: 'Enter your password.',
  wrong: 'That username or password is not right. Please try again.',
  tooMany: 'Too many tries. Wait a few minutes, then try again.',
  offline: "Can't connect. Check the internet and try again.",
  failed: "Couldn't sign in. Please try again.",
  footnote: 'For State Life staff only. Everything you do here is recorded.',
  statusLink: 'See device status without signing in',
} as const

export function loginError(reason: unknown): string {
  if (reason instanceof ApiError) {
    if (reason.status === 401 || reason.status === 403) return LOGIN_WORDS.wrong
    if (reason.status === 429) return LOGIN_WORDS.tooMany
    return LOGIN_WORDS.failed
  }
  if (reason instanceof TypeError) return LOGIN_WORDS.offline
  return LOGIN_WORDS.failed
}

export default function Login({ onLogin, notice }: { onLogin: (username: string, password: string) => Promise<void>; notice?: string | null }) {
  const [error, setError] = useState('')
  const [shake, setShake] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const cardRef = useRef<HTMLFormElement>(null)
  const schema = z.object({ username: z.string().trim().min(1, LOGIN_WORDS.needUsername), password: z.string().min(1, LOGIN_WORDS.needPassword) })
  type LoginValues = z.infer<typeof schema>
  const { register, handleSubmit, setFocus, getValues, formState: { isSubmitting, errors } } = useForm<LoginValues>({
    defaultValues: { username: '', password: '' },
  })
  // Focus the first empty field (autofill may have filled the username).
  useEffect(() => {
    const timer = window.setTimeout(() => setFocus(getValues('username') ? 'password' : 'username'), 0)
    return () => window.clearTimeout(timer)
  }, [getValues, setFocus])
  const submit = handleSubmit(async (values) => {
    setError('')
    const parsed = schema.safeParse(values)
    if (!parsed.success) return
    try {
      await onLogin(parsed.data.username, parsed.data.password)
    } catch (reason) {
      setError(loginError(reason))
      setShake(true)
      window.setTimeout(() => setShake(false), 320)
      setFocus('password')
    }
  })
  return (
    <main className="login-shell">
      <form ref={cardRef} className={`login-card${shake ? ' is-shaking' : ''}`} onSubmit={(event) => void submit(event)} noValidate aria-labelledby="login-title">
        <img src="/state-life-logo.png" alt="State Life Insurance Corporation" />
        <h1 id="login-title">{LOGIN_WORDS.title}</h1>
        <p className="supporting">{LOGIN_WORDS.intro}</p>
        {notice && (
          <div className="message pattern-notice login-notice" role="status">
            <Icon name="info" /> {notice}
          </div>
        )}
        <div className="login-field">
          <label htmlFor="login-username">{LOGIN_WORDS.username}</label>
          <input
            id="login-username"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            {...register('username', { validate: (value) => value.trim().length > 0 || LOGIN_WORDS.needUsername })}
            aria-invalid={Boolean(errors.username)}
            aria-describedby={errors.username ? 'login-username-error' : undefined}
          />
          {errors.username && <small id="login-username-error" className="field-error">{errors.username.message}</small>}
        </div>
        <div className="login-field">
          <label htmlFor="login-password">{LOGIN_WORDS.password}</label>
          <span className="login-password">
            <input
              id="login-password"
              type={showPassword ? 'text' : 'password'}
              autoComplete="current-password"
              enterKeyHint="done"
              {...register('password', { required: LOGIN_WORDS.needPassword })}
              aria-invalid={Boolean(errors.password)}
              aria-describedby={errors.password ? 'login-password-error' : undefined}
            />
            <button type="button" className="text-button" aria-pressed={showPassword} aria-label={LOGIN_WORDS.showPassword} onClick={() => setShowPassword((value) => !value)}>
              {showPassword ? LOGIN_WORDS.hide : LOGIN_WORDS.show}
            </button>
          </span>
          {errors.password && <small id="login-password-error" className="field-error">{errors.password.message}</small>}
        </div>
        {error && (
          <div className="message pattern-blocked" role="alert">
            <Icon name="alert" /> {error}
          </div>
        )}
        <button className="button primary full large" disabled={isSubmitting} aria-busy={isSubmitting || undefined}>
          {isSubmitting && <span className="login-spinner" aria-hidden="true" />}
          {isSubmitting ? LOGIN_WORDS.signingIn : LOGIN_WORDS.signIn}
        </button>
        <p className="login-footnote"><Icon name="shield" /> {LOGIN_WORDS.footnote}</p>
      </form>
      <a className="login-status-link" href="/status">{LOGIN_WORDS.statusLink} <span aria-hidden="true">→</span></a>
    </main>
  )
}
