import { describe, expect, it } from 'vitest'
import { DASHBOARD_ROUTES } from './routing'
import { routeTable } from './routeTable'
import { isPlainText } from './words/glossary'
import { MENU_WORDS } from './words/home'

describe('route table (DLV-4)', () => {
  it('has exactly one row per page, with a plain title that matches the menu', () => {
    expect(Object.keys(routeTable).sort()).toEqual([...DASHBOARD_ROUTES].sort())
    for (const route of DASHBOARD_ROUTES) {
      const entry = routeTable[route]
      expect(entry.route).toBe(route)
      expect(isPlainText(entry.title)).toBe(true)
      expect(entry.title).toBe(MENU_WORDS[route].label)
    }
    expect(Object.keys(routeTable)).not.toContain('status')
  })
})
