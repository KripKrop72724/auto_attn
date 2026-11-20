// Browser half of the plain-words guard (FND-1). Report-only: hits are added
// to the test as annotations so the PR shows them; WORDS-11 lists the pages
// that are already strict in STRICT_ROUTES.
import type { Page, TestInfo } from '@playwright/test'
import { findPlainWordsProblems, type PlainWordsProblem } from '../../src/test/plainWords'
import { BANNED_WORDS } from '../../src/words/glossary'

// Every page except Set up a device, whose browser wizard keeps its engineer
// words until ADV-20 (the e2e fixture has can_start=true, so the wizard shows).
export const STRICT_ROUTES: readonly string[] = [
  '/home', '/devices', '/devices/connector-one', '/attendance', '/problems', '/people', '/people/connector-one',
  '/recover', '/updates', '/settings',
]

/** Collects the text a person can read on the page (marked IT parts left out). */
export async function pageVisibleText(page: Page): Promise<string> {
  return page.evaluate(() => {
    const skip = '[data-technical], [data-device-code], [data-brand], [data-user-text], [data-typed-phrase], [data-old-name], script, style, noscript, [hidden], [aria-hidden="true"]'
    const parts: string[] = []
    const walk = (node: Node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        const value = node.textContent?.trim()
        if (value) parts.push(value)
        return
      }
      if (node instanceof Element) {
        if (node.matches(skip)) return
        const style = getComputedStyle(node)
        if (style.display === 'none' || style.visibility === 'hidden') return
        for (const attribute of ['aria-label', 'title', 'placeholder', 'alt']) {
          const value = node.getAttribute(attribute)?.trim()
          if (value) parts.push(value)
        }
      }
      node.childNodes.forEach(walk)
    }
    walk(document.body)
    return parts.join('\n')
  })
}

/** Adds one annotation per problem found on the page; never fails the test. */
export async function reportPlainWords(page: Page, testInfo: TestInfo, label: string, banned: readonly string[] = BANNED_WORDS): Promise<PlainWordsProblem[]> {
  const problems = findPlainWordsProblems(await pageVisibleText(page), banned)
  const unique = [...new Map(problems.map((problem) => [`${problem.rule}:${problem.text}`, problem])).values()]
  for (const problem of unique.slice(0, 40)) {
    testInfo.annotations.push({ type: 'plain-words', description: `${label}: ${problem.rule} "${problem.text}"` })
  }
  return unique
}
