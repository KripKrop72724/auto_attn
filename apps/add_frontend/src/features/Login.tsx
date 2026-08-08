import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { Icon } from '../Icon'

export default function Login({ onLogin }: { onLogin: (username: string, password: string) => Promise<void> }) {
  const [error, setError] = useState('')
  const schema = z.object({ username: z.string().trim().min(1, 'Username is required.'), password: z.string().min(1, 'Password is required.') })
  type LoginValues = z.infer<typeof schema>
  const { register, handleSubmit, watch, formState: { isSubmitting, errors } } = useForm<LoginValues>({
    defaultValues: { username: 'StateHealthAdmin', password: '' },
  })
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
      <section className="login-brand" aria-label="State Life Attendance Device Dashboard">
        <img src="/state-life-logo.png" alt="State Life Insurance Corporation" />
        <p className="eyebrow inverse">ATTENDANCE DEVICE OPERATIONS</p>
        <h1>One trusted view of every attendance terminal.</h1>
        <p>
          Command, control, and surveillance for authorized Zone Lite devices and their ZKT
          terminals across Pakistan.
        </p>
        <div className="brand-proof">
          <span><Icon name="shield" /> Signed device identity</span>
          <span><Icon name="terminal" /> Durable command ledger</span>
          <span><Icon name="pulse" /> Live attendance oversight</span>
        </div>
      </section>
      <section className="login-form-side">
        <form className="login-card" onSubmit={(event) => void submit(event)} noValidate>
          <div className="compact-brand">
            <img src="/state-life-logo.png" alt="" />
            <span>State Life Insurance Corporation</span>
          </div>
          <p className="eyebrow">AUTHORIZED ACCESS</p>
          <h2>Attendance Device Dashboard</h2>
          <p className="supporting">Sign in to the national device operations console.</p>
          <label>
            Username
            <input
              autoComplete="username"
              {...register('username', { required: 'Username is required.' })}
              aria-invalid={Boolean(errors.username)}
            />
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete="current-password"
              {...register('password', { required: 'Password is required.' })}
              aria-invalid={Boolean(errors.password)}
            />
          </label>
          {error && (
            <div className="message pattern-blocked" role="alert">
              <Icon name="alert" /> {error}
            </div>
          )}
          <button className="button primary full" disabled={isSubmitting || !watch('username') || !watch('password')}>
            {isSubmitting ? 'Authenticating…' : 'Enter dashboard'} <Icon name="chevron" />
          </button>
          <small>Authorized State Life personnel only. Every operation is audited.</small>
        </form>
      </section>
    </main>
  )
}
