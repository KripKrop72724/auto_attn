import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import chipSource from './StatusChip.tsx?raw'
import { StatusChip, toneForPattern } from './StatusChip'
import { DivisionTag } from './DivisionTag'
import { Badge, capCount } from './Badge'
import { setUpKitDom, tearDownKitDom } from './testUtils'

describe('StatusChip', () => {
  let container: HTMLElement
  beforeEach(() => { container = setUpKitDom() })
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('shows the plain label with an icon, speaks it and keeps the code out of sight', () => {
    render(<StatusChip tone="attention" label="Waiting for the machine" code="WAITING_FOR_ZKT" />, { container })
    const chip = screen.getByLabelText('Status: Waiting for the machine')
    expect(chip.textContent).toBe('Waiting for the machine')
    expect(chip.querySelector('svg')).not.toBeNull()
    expect(chip.getAttribute('data-status-code')).toBe('WAITING_FOR_ZKT')
    expect(chip.getAttribute('data-pattern')).toBe('waiting')
    expect(chip.getAttribute('data-tone')).toBe('attention')
    expect(chip.getAttribute('title')).toBeNull()
    expect(chip.textContent).not.toMatch(/[A-Z]+_[A-Z]+/)
  })

  it('never animates or truncates, so long labels show in full', () => {
    render(<StatusChip tone="neutral" label="Machine back, checking it stays connected" />, { container })
    const chip = screen.getByLabelText('Status: Machine back, checking it stays connected')
    expect(chip.querySelector('i')).toBeNull()
    expect(chip.className).toBe('ui-chip tone-neutral')
    expect(chip.querySelectorAll('span')).toHaveLength(1)
  })

  it('maps the old status patterns to tones and can drop the prefix', () => {
    expect(toneForPattern('confirmed')).toBe('working')
    expect(toneForPattern('blocked')).toBe('offline')
    expect(toneForPattern('waiting')).toBe('attention')
    expect(toneForPattern('notice')).toBe('neutral')
    render(<StatusChip tone="working" label="Working" labelPrefix={null} icon={null} />, { container })
    const chip = screen.getByText('Working').parentElement as HTMLElement
    expect(chip.getAttribute('aria-label')).toBeNull()
    expect(chip.querySelector('svg')).toBeNull()
  })

  it('does not import the words dictionaries', () => {
    expect(chipSource).not.toMatch(/from ['"][./]*words/)
  })
})

describe('DivisionTag and Badge', () => {
  let container: HTMLElement
  beforeEach(() => { container = setUpKitDom() })
  afterEach(() => { cleanup(); tearDownKitDom() })

  it('always says Life or Health in words', () => {
    render(<><DivisionTag division="life" /><DivisionTag division="health" /></>, { container })
    expect(screen.getByText('Life').getAttribute('data-division')).toBe('life')
    expect(screen.getByText('Health').className).toContain('is-health')
  })

  it('caps counts at 99+ and keeps hidden text inside the badge', () => {
    expect(capCount(6)).toBe('6')
    expect(capCount(150)).toBe('99+')
    render(<Badge tone="danger" hiddenText="6 problems to look at">6</Badge>, { container })
    expect(screen.getByText('6 problems to look at').className).toBe('sr-only')
  })
})
