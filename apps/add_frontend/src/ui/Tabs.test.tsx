import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { useState } from 'react'
import { SegmentedControl, TabPanel, Tabs } from './Tabs'
import { DivisionScopeChip, DivisionSwitch, type DivisionChoice } from './DivisionSwitch'
import { setUpKitDom, tearDownKitDom } from './testUtils'

type TabId = 'new' | 'seen' | 'fixed'

// Pages keep the tab in the URL (?tab=); the harness mirrors that contract.
function UrlTabs() {
  const read = () => (new URLSearchParams(window.location.search).get('tab') as TabId) || 'new'
  const [tab, setTab] = useState<TabId>(read)
  const change = (next: TabId) => {
    window.history.replaceState(null, '', `?tab=${next}`)
    setTab(next)
  }
  return <>
    <Tabs idPrefix="problems" label="Problems" value={tab} onChange={change} tabs={[{ id: 'new', label: 'New', count: 1200 }, { id: 'seen', label: 'Seen' }, { id: 'fixed', label: 'Fixed' }]} />
    <TabPanel idPrefix="problems" id={tab}>Panel {tab}</TabPanel>
  </>
}

describe('Tabs and SegmentedControl', () => {
  let container: HTMLElement
  beforeEach(() => {
    container = setUpKitDom()
    window.history.replaceState(null, '', '/problems')
  })
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('follows the WAI-ARIA tabs pattern with arrow keys, Home and End, and writes ?tab=', () => {
    render(<UrlTabs />, { container })
    const list = screen.getByRole('tablist', { name: 'Problems' })
    const first = screen.getByRole('tab', { name: /New/ })
    expect(first.getAttribute('aria-selected')).toBe('true')
    expect(first.textContent).toContain('1,200')
    expect(screen.getByRole('tabpanel').getAttribute('aria-labelledby')).toBe(first.id)
    fireEvent.keyDown(list, { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: 'Seen' }).getAttribute('aria-selected')).toBe('true')
    expect(window.location.search).toBe('?tab=seen')
    fireEvent.keyDown(list, { key: 'End' })
    expect(window.location.search).toBe('?tab=fixed')
    fireEvent.keyDown(list, { key: 'ArrowRight' })
    expect(window.location.search).toBe('?tab=new')
    fireEvent.keyDown(list, { key: 'ArrowLeft' })
    expect(window.location.search).toBe('?tab=fixed')
    fireEvent.keyDown(list, { key: 'Home' })
    expect(screen.getByRole('tabpanel').textContent).toBe('Panel new')
    expect(screen.getAllByRole('tab').filter((tab) => tab.tabIndex === 0)).toHaveLength(1)
  })

  it('reads the tab from the URL on first render', () => {
    window.history.replaceState(null, '', '/problems?tab=seen')
    render(<UrlTabs />, { container })
    expect(screen.getByRole('tab', { name: 'Seen' }).getAttribute('aria-selected')).toBe('true')
  })

  it('makes a radio group that moves with arrow keys', () => {
    function Harness() {
      const [value, setValue] = useState<'a' | 'b'>('a')
      return <SegmentedControl label="View" value={value} onChange={setValue} options={[{ id: 'a', label: 'List' }, { id: 'b', label: 'Cards' }]} />
    }
    render(<Harness />, { container })
    const group = screen.getByRole('radiogroup', { name: 'View' })
    fireEvent.keyDown(group, { key: 'ArrowRight' })
    expect(screen.getByRole('radio', { name: 'Cards' }).getAttribute('aria-checked')).toBe('true')
  })

  it('offers All, Life and Health and says what is shown', () => {
    function Harness() {
      const [division, setDivision] = useState<DivisionChoice>('all')
      return <><DivisionSwitch value={division} onChange={setDivision} /><DivisionScopeChip division={division} onShowAll={() => setDivision('all')} otherSummary="Health: 1 offline" /></>
    }
    render(<Harness />, { container })
    expect(screen.getByRole('radiogroup', { name: 'Division' }).className).toContain('division-switch')
    expect(screen.queryByText('Showing Life only')).toBeNull()
    fireEvent.click(screen.getByRole('radio', { name: 'Life' }))
    expect(screen.getByText('Showing Life only')).toBeTruthy()
    expect(screen.getByText('Health: 1 offline')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Show all' }))
    expect(screen.getByRole('radio', { name: 'All' }).getAttribute('aria-checked')).toBe('true')
    expect(screen.queryByText('Showing Life only')).toBeNull()
  })
})
