// A fresh key for every write the person starts, so a retried request is never applied twice.
export const idempotency = (prefix: string) => {
  const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
  return `${prefix}:${id}`
}
