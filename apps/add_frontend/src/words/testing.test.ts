import { describe, expect, it } from 'vitest'
import { expectPlainText, plainTextProblems } from './testing'

const html = (markup: string) => {
  const div = document.createElement('div')
  div.innerHTML = markup
  return div
}

describe('expectPlainText', () => {
  it('passes plain text and skips marked elements', () => {
    expect(() => expectPlainText(html(`
      <h2>Link box cannot save attendance safely</h2>
      <button aria-label="Mark as seen">Mark as seen</button>
      <details data-technical><summary>Technical details</summary><code>ESP_DURABILITY_FAULT</code> reason=local_write_commit</details>
      <span data-device-code>LF-ZONE-BLD9-03</span>
      <span data-brand>Hikvision</span> <span data-brand>ADD</span>
      <span data-user-text>Moved by ZKT_ADMIN</span>
    `))).not.toThrow()
  })

  it('fails on codes, system words and attributes outside Technical details', () => {
    const container = html(`
      <p>ESP_DURABILITY_FAULT · First 30-Oct-2026</p>
      <span title="Status: ONLINE WITH WARNINGS">Online</span>
      <input placeholder="Search name, zone, MAC, or serial" />
      <p>Waiting for live Zone Lite logs…</p>
    `)
    const problems = plainTextProblems(container)
    expect(problems.map((p) => p.match)).toEqual(expect.arrayContaining(['ESP_DURABILITY_FAULT', 'Zone Lite', 'ONLINE', 'MAC']))
    expect(problems.find((p) => p.where === 'title')).toBeTruthy()
    expect(() => expectPlainText(container)).toThrow(/Plain words guard/)
  })

  it('accepts an HTML string and extra skip selectors', () => {
    expect(() => expectPlainText('<pre class="raw">[HIK_POLL_STATUS] Poll OK</pre>', { skip: '.raw' })).not.toThrow()
    expect(() => expectPlainText('<p>Poll OK; reason=ok</p>')).toThrow()
  })
})
