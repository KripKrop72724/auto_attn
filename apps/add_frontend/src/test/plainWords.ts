// The plain-words guard (FND-1, report-only for now; WORDS-11 makes it strict
// page by page). It finds system words in what a person sees: anything inside
// an element marked data-technical, data-device-code or data-brand is skipped.
// The words chapter passes its BANNED_WORDS list (fe/words/glossary.ts) in.

export interface PlainWordsProblem {
  rule: string
  text: string
}

const RULES: ReadonlyArray<{ rule: string; pattern: RegExp }> = [
  { rule: 'snake-case code', pattern: /\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g },
  { rule: 'key=value', pattern: /\b[a-z_]{2,}=[^\s]+/gi },
  { rule: 'hash or digest', pattern: /\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b/gi },
  { rule: 'MAC address', pattern: /\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b/gi },
  { rule: 'IP address', pattern: /\b(?:\d{1,3}\.){3}\d{1,3}\b/g },
  { rule: 'build string', pattern: /\bzone-lite-\d+\.\d+\.\d+\b/gi },
  { rule: 'raw request error', pattern: /Request failed \(\d{3}\)/g },
  { rule: 'file name', pattern: /\b[\w-]+\.(?:tsx?|py|c|json|ya?ml|bin)\b/g },
]

/** Problems in one piece of visible text. `banned` adds whole words (case-insensitive). */
export function findPlainWordsProblems(text: string, banned: readonly string[] = []): PlainWordsProblem[] {
  const problems: PlainWordsProblem[] = []
  for (const { rule, pattern } of RULES) {
    for (const match of text.matchAll(pattern)) problems.push({ rule, text: match[0] })
  }
  for (const word of banned) {
    const escaped = word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const pattern = new RegExp(`(?<![\\w-])${escaped}(?![\\w-])`, 'gi')
    for (const match of text.matchAll(pattern)) problems.push({ rule: 'banned word', text: match[0] })
  }
  return problems
}

const SKIP = '[data-technical], [data-device-code], [data-brand], script, style, noscript, [hidden], [aria-hidden="true"]'

/** The text a person can read in a container, without the parts marked for IT. */
export function visibleText(container: ParentNode): string {
  const parts: string[] = []
  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const value = node.textContent?.trim()
      if (value) parts.push(value)
      return
    }
    if (node instanceof Element) {
      if (node.matches(SKIP)) return
      for (const attribute of ['aria-label', 'title', 'placeholder', 'alt']) {
        const value = node.getAttribute(attribute)?.trim()
        if (value) parts.push(value)
      }
    }
    node.childNodes.forEach(walk)
  }
  container.childNodes.forEach(walk)
  return parts.join('\n')
}

/** Report-only check for unit tests: returns the problems instead of failing. */
export function plainWordsReport(container: ParentNode = document.body, banned: readonly string[] = []) {
  return findPlainWordsProblems(visibleText(container), banned)
}
