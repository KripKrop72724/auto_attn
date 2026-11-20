import { useEffect, useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { Icon } from '../Icon'

export default function Login({ onLogin }: { onLogin: (username: string, password: string) => Promise<void> }) {
  const [error, setError] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const schema = z.object({ username: z.string().trim().min(1, 'Username is required.'), password: z.string().min(1, 'Password is required.') })
  type LoginValues = z.infer<typeof schema>
  const { register, handleSubmit, setFocus, formState: { isSubmitting, errors } } = useForm<LoginValues>({
    defaultValues: { username: 'StateHealthAdmin', password: '' },
  })
  useEffect(() => { setFocus('password') }, [setFocus])
  const submit = handleSubmit(async (values) => {
    setError('')
    const parsed = schema.safeParse(values)
    if (!parsed.success) return
    try {
      await onLogin(parsed.data.username, parsed.data.password)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Sign in failed.')
    }
  })
  return (
    <main className="login-shell">
      <form className="login-card" onSubmit={(event) => void submit(event)} noValidate aria-labelledby="login-title">
        <img src="/state-life-logo.png" alt="State Life Insurance Corporation" />
        <h1 id="login-title">Attendance Device Dashboard</h1>
        <p className="supporting">Sign in to the national device operations console.</p>
        <div className="login-field">
          <label htmlFor="login-username">Username</label>
          <input
            id="login-username"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            {...register('username', { required: 'Username is required.', validate: (value) => value.trim().length > 0 || 'Username is required.' })}
            aria-invalid={Boolean(errors.username)}
            aria-describedby={errors.username ? 'login-username-error' : undefined}
          />
          {errors.username && <small id="login-username-error" className="field-error">{errors.username.message}</small>}
        </div>
        <div className="login-field">
          <label htmlFor="login-password">Password</label>
          <span className="login-password">
            <input
              id="login-password"
              type={showPassword ? 'text' : 'password'}
              autoComplete="current-password"
              {...register('password', { required: 'Password is required.' })}
              aria-invalid={Boolean(errors.password)}
              aria-describedby={errors.password ? 'login-password-error' : undefined}
            />
            <button type="button" className="text-button" aria-pressed={showPassword} aria-label="Show password" onClick={() => setShowPassword((value) => !value)}>
              {showPassword ? 'Hide' : 'Show'}
            </button>
          </span>
          {errors.password && <small id="login-password-error" className="field-error">{errors.password.message}</small>}
        </div>
        {error && (
          <div className="message pattern-blocked" role="alert">
            <Icon name="alert" /> {error}
          </div>
        )}
        <button className="button primary full large" disabled={isSubmitting}>
          {isSubmitting ? 'Signing in…' : 'Sign in'}
        </button>
        <p className="login-footnote"><Icon name="shield" /> Authorized State Life personnel only. Every operation is audited.</p>
      </form>
    </main>
  )
}
