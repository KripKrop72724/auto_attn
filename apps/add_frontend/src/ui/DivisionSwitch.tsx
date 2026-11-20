import { SegmentedControl } from './Tabs'
import { UI_TEXT } from './uiText'
import { cx } from './cx'
import './tabs.css'

export type DivisionChoice = 'all' | 'life' | 'health'

const options: { id: DivisionChoice; label: string }[] = [
  { id: 'all', label: UI_TEXT.all },
  { id: 'life', label: UI_TEXT.life },
  { id: 'health', label: UI_TEXT.health },
]

// All · Life · Health. The frame keeps the value in the URL (?division=) and
// the preferences module; this part only shows and changes it.
export function DivisionSwitch({ value, onChange, className, fullWidth = false }: { value: DivisionChoice; onChange: (value: DivisionChoice) => void; className?: string; fullWidth?: boolean }) {
  return <SegmentedControl options={options} value={value} onChange={onChange} label={UI_TEXT.division} className={cx('division-switch', className)} fullWidth={fullWidth} />
}

// "Showing Life only · Health: 1 offline · Show all", only when a division is
// chosen. `otherSummary` is the caller's plain sentence about the hidden one.
export function DivisionScopeChip({ division, onShowAll, otherSummary, className }: { division: DivisionChoice; onShowAll: () => void; otherSummary?: string | null; className?: string }) {
  if (division === 'all') return null
  const name = division === 'life' ? UI_TEXT.life : UI_TEXT.health
  return (
    <p className={cx('ui-division-scope', className)} data-division={division}>
      <span>{UI_TEXT.showingOnly(name)}</span>
      {otherSummary && <><span aria-hidden="true"> · </span><span>{otherSummary}</span></>}
      <span aria-hidden="true"> · </span>
      <button type="button" className="text-button ui-division-scope-all" onClick={onShowAll}>{UI_TEXT.showAll}</button>
    </p>
  )
}
