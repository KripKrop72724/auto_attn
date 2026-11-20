// Page-test helper for the plain-words guard (02-words WORDS-11, test 7).
//
//   import { expectPlainText } from '../words/testing'
//   const { container } = render(<Page />)
//   expectPlainText(container)
//
// Walks every visible text node and the aria-label, title, placeholder and alt
// attributes, skipping anything inside [data-technical], [data-brand],
// [data-device-code] or [data-user-text], and throws when it finds a
// SNAKE_CASE token, key=value, JSON, a hash, MAC, IP, file name, device code,
// "(s)", "!" or a banned system word. Not imported by app code.

import { findPlainWordProblems, type PlainWordProblem } from './glossary'

export const SKIP_SELECTOR = '[data-technical], [data-brand], [data-device-code], [data-user-text], [data-typed-phrase], [data-old-name]'
const ATTRIBUTES = ['aria-label', 'title', 'placeholder', 'alt'] as const
const IGNORED_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'])

export interface PlainTextFinding extends PlainWordProblem {
  /** The text or attribute value the problem was found in. */
  text: string
  /** "text" or the attribute name. */
  where: string
  /** A short path to the element, for the failure message. */
  element: string
}

export interface PlainTextOptions {
  /** Extra selector of elements to skip (for example a raw log you test elsewhere). */
  skip?: string
  /** Report only these rules (default: all). */
  rules?: PlainWordProblem['rule'][]
}

const describeElement = (element: Element): string => {
  const parts: string[] = []
  let current: Element | null = element
  while (current && parts.length < 3) {
    const id = current.id ? `#${current.id}` : ''
    const cls = typeof current.className === 'string' && current.className.trim() ? `.${current.className.trim().split(/\s+/)[0]}` : ''
    parts.unshift(`${current.tagName.toLowerCase()}${id}${cls}`)
    current = current.parentElement
  }
  return parts.join(' > ')
}

const skipped = (element: Element | null, selector: string) => Boolean(element?.closest(selector))

/** Every plain-words problem in a rendered container (or an HTML string). */
export function plainTextProblems(root: Element | Document | string, options: PlainTextOptions = {}): PlainTextFinding[] {
  let container: Element | Document
  if (typeof root === 'string') {
    const template = document.createElement('template')
    template.innerHTML = root
    const host = document.createElement('div')
    host.appendChild(template.content.cloneNode(true))
    container = host
  } else {
    container = root
  }
  const selector = options.skip ? `${SKIP_SELECTOR}, ${options.skip}` : SKIP_SELECTOR
  const findings: PlainTextFinding[] = []
  const keep = (problem: PlainWordProblem) => !options.rules || options.rules.includes(problem.rule)
  const doc = container instanceof Document ? container : container.ownerDocument ?? document
  const walker = doc.createTreeWalker(container instanceof Document ? container.body ?? container : container, NodeFilter.SHOW_TEXT)
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = node.parentElement
    if (!parent || IGNORED_TAGS.has(parent.tagName) || skipped(parent, selector)) continue
    const text = (node.textContent ?? '').replace(/\s+/g, ' ').trim()
    if (!text) continue
    for (const problem of findPlainWordProblems(text)) {
      if (keep(problem)) findings.push({ ...problem, text, where: 'text', element: describeElement(parent) })
    }
  }
  const elements = container instanceof Document ? container.querySelectorAll('*') : [container, ...Array.from(container.querySelectorAll('*'))]
  for (const element of Array.from(elements) as Element[]) {
    if (IGNORED_TAGS.has(element.tagName) || skipped(element, selector)) continue
    for (const attribute of ATTRIBUTES) {
      const value = element.getAttribute(attribute)
      if (!value) continue
      for (const problem of findPlainWordProblems(value)) {
        if (keep(problem)) findings.push({ ...problem, text: value, where: attribute, element: describeElement(element) })
      }
    }
  }
  return findings
}

/** Throws (failing the test) when the container shows anything that isn't plain words. */
export function expectPlainText(root: Element | Document | string, options: PlainTextOptions = {}): void {
  const findings = plainTextProblems(root, options)
  if (!findings.length) return
  const lines = findings.slice(0, 25).map((f) => `  - ${f.why}: "${f.match}" in ${f.where} "${f.text.slice(0, 120)}" (${f.element})`)
  const more = findings.length > 25 ? `\n  … and ${findings.length - 25} more` : ''
  throw new Error(
    `Plain words guard: ${findings.length} problem${findings.length === 1 ? '' : 's'} outside Technical details.\n` +
      `${lines.join('\n')}${more}\n` +
      'Use a words/ dictionary for codes, or put engineer text inside an element marked data-technical.',
  )
}

/** Alias with the name the plan uses (02-words test 7: expectPlainWords). */
export const expectPlainWords = expectPlainText
