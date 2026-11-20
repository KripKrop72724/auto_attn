import { describe, expect, it } from 'vitest'
import conf from '../../security-headers.conf?raw'
import viteConfig from '../../vite.config.ts?raw'

// The e2e server must send the same Content Security Policy as production,
// or a policy break would only show on the live site (FND-1).
describe('preview security headers', () => {
  it('match the production nginx policy', () => {
    const policy = /Content-Security-Policy "([^"]+)"/.exec(conf)?.[1]
    expect(policy).toBeTruthy()
    expect(viteConfig).toContain(`'Content-Security-Policy': "${policy}"`)
    for (const header of ['X-Content-Type-Options', 'X-Frame-Options', 'Referrer-Policy', 'Permissions-Policy']) {
      const value = new RegExp(`${header} "([^"]+)"`).exec(conf)?.[1]
      expect(value).toBeTruthy()
      expect(viteConfig).toContain(`'${header}': '${value}'`)
    }
  })
})
