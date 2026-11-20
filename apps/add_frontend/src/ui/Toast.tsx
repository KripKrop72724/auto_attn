import { useCallback, useEffect, useMemo, useState } from 'react'

export type ToastState = { kind: 'notice' | 'error'; text: string } | null

export function useToast() {
  const [toast, setToast] = useState<ToastState>(null)
  useEffect(() => {
    if (!toast) return
    const timeout = window.setTimeout(() => setToast(null), toast.kind === 'error' ? 8000 : 5000)
    return () => window.clearTimeout(timeout)
  }, [toast])
  const notice = useCallback((text: string) => setToast({ kind: 'notice', text }), [])
  const error = useCallback((text: string) => setToast({ kind: 'error', text }), [])
  const dismiss = useCallback(() => setToast(null), [])
  return useMemo(() => ({ toast, notice, error, dismiss }), [dismiss, error, notice, toast])
}

export type Toast = ReturnType<typeof useToast>
