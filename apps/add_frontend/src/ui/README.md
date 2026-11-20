# ADD component kit (`src/ui`)

Import from `./ui` (or `../ui` in `features/`): `import { Button, StatusChip } from '../ui'`.
Core styles (buttons, tones, dialog, toast) live in `ui/ui.css`, pulled into the entry stylesheet by
`styles.css` (`@import './ui/ui.css'`). Every other part imports its own CSS file (`chips.css`, `content.css`,
`tabs.css`, `sheet.css`, `filterbar.css`, `datalist.css`, `steps.css`), so it ships with the page that uses it and
the 14 KB entry CSS budget stays clear. Nothing else needs importing. Tokens (colour, type, spacing, radius, shadow, `--z-*`, motion) are in
`styles.css :root`; use them instead of literals.

Rules every part follows: 44 px touch targets on phones and touch screens, visible focus rings, AA contrast,
colour never the only signal (icon + words), no hover-only `title`, motion off under
`prefers-reduced-motion`. **The kit never adds an "Are you sure?", a reason box, a password or a typed phrase**:
one-click actions stay one click. The only question it ever asks is "Discard your changes?" when a dialog or
sheet with typed text (`dirty`) is closed.

**Words.** The kit shows words, it does not choose them. Status labels, codes and sentences come from
`fe/words/*` through the caller. The kit's own fixed control words (Close, Try again, Technical details, Copy,
More filters, Clear all, Waiting / Sent to machine / Confirmed …) are in `uiText.ts` (`UI_TEXT`).

Breakpoints: phone ≤760 px, tablet 761–1023 px, desktop ≥1024 px. `useIsPhone()` reads the phone query.

## Actions

| Component | Props |
|---|---|
| `Button` | `variant` `'primary' \| 'secondary' (default) \| 'danger' \| 'danger-outline' \| 'ghost' \| 'link'`; `size` `'sm' \| 'md' \| 'lg'`; `busy`; `busyLabel` ("Saving…"); `doneLabel` (tick + text 1.2 s after an `onClick` promise resolves); `icon`, `iconEnd` (Icon names); `fullWidth`; `onClick` — if it returns a promise the button is busy until it settles (rejections are swallowed: show the error yourself). Busy = spinner, `aria-busy`, disabled, width kept. Uses the old `.button primary` etc. classes so pages keep their look. Forwards `ref`. |
| `IconButton` | `label` (required accessible name), `icon`, `busy`, plus button attributes. Forwards `ref`. |
| `ActionMenu` | `items: { id, label, icon?, href?, onSelect?, danger?, disabled? }[]`, `label` (default "More actions"). The ⋯ button and its menu (arrow keys, Home/End, Escape). |
| `Spinner` | `size` `'sm' \| 'md'`; `label` makes it a polite `role="status"` with hidden text. |

## Status and labels

| Component | Props |
|---|---|
| `StatusChip` | `tone` `'working' \| 'attention' \| 'offline' \| 'neutral' \| 'info'`; `label` (plain words from `fe/words`); `code?` (raw code → `data-status-code`, never shown); `icon?` (override, `null` hides); `labelPrefix` (default `'Status'` → `aria-label="Status: {label}"`, `null` for none); `size` `'sm' \| 'md'`. Icon + text, one row, never truncates, no pulse, no `title`. `data-pattern` keeps the old confirmed/waiting/blocked/notice value. Helpers `toneForPattern(pattern)` / `patternForTone(tone)` bridge `statusPattern()` from `status.ts`. |
| `DivisionTag` | `division` `'life' \| 'health'`. Shows "Life" / "Health" with a square marker; Life indigo, Health violet (D-11). |
| `Badge` | `tone` `'neutral' \| 'info' \| 'danger'`; `children` (count or short word such as "Switched off"); `hiddenText` (full sentence for screen readers, kept inside the element). `capCount(n, 99)` → "99+". |

## Layout

| Component | Props |
|---|---|
| `PageHeader` | `title` (h1, `tabIndex={-1}`); `description?` (one sentence, hidden on phones, class `.page-header-description`); `primaryAction?` (one button; `action` is accepted as the old name); `moreActions?: ActionMenuItem[]` (the ⋯ menu); `children` (under the title, e.g. `DivisionScopeChip`); `headingRef` (the frame focuses it after navigation). |
| `Card` | `title?`, `subtitle?`, `actions?`, `tone?` (coloured left edge; say the state in words too), `headingLevel` 2–4 (default 2), `as` `'section' \| 'article' \| 'div'`. |
| `StatTile` | `label`; `value: number \| string \| null` (numbers "158,560" en-PK, `null` → "–"); `subtitle?`; `tone?`; `icon?`; `href?` and/or `onClick?` (a link; the chevron always shows; call `event.preventDefault()` in `onClick` to route in-app); `loading` (skeleton, never a fake 0). Value change = 600 ms tint. |
| `Tabs` | `tabs: { id, label, count?, disabled? }[]`; `value`; `onChange`; `label` (accessible name); `idPrefix` (pairs with `TabPanel`). WAI-ARIA tabs (arrows, Home/End, roving tabindex), counts, sliding underline, edge fades when they scroll on a phone, selected tab scrolled into view. Keep the value in the URL (`?tab=`) with `useUrlState`. |
| `TabPanel` | `idPrefix`, `id`, `children`. |
| `SegmentedControl` | `options: { id, label }[]`, `value`, `onChange`, `label`, `fullWidth?`. `role="radiogroup"`, arrows move and choose, sliding thumb. |
| `DivisionSwitch` | `value: 'all' \| 'life' \| 'health'`, `onChange`, `fullWidth?` (phone row). Group name "Division", class `.division-switch`. The frame owns the URL / preference. |
| `DivisionScopeChip` | `division`, `onShowAll`, `otherSummary?` ("Health: 1 offline, 2 new problems"). Renders nothing for `'all'`; otherwise "Showing Life only · … · Show all". |

## Feedback

| Component | Props |
|---|---|
| `Skeleton` | `variant` `'line' \| 'block' \| 'tile' \| 'row'`, `count`, `width?`, `label?` (one hidden "Loading …" status for the region). |
| `EmptyState` | `icon?` (default `list`, `null` hides), `title` (default "Nothing here yet"), `children` (one sentence), `action?`, `headingLevel`. |
| `ErrorState` | `title` (default "Couldn't load this"); `message?` (plain sentence, e.g. from `plainApiError()`); `error?` (reads `plainMessage` for the sentence when no `message`, and puts `status`, `requestId` as "Reference: …" and the raw `message` into Technical details — never into the visible text); `onRetry?` (may return a promise → "Trying…"); `retryLabel`, `busyLabel`; `technical?` (extra technical content); `compact`; `headingLevel`. `role="alert"`. |
| `Callout` | `tone` `'info' \| 'success' \| 'warning' \| 'danger'`, `title?`, `children`, `action?`, `icon?`, `role?`. |
| `Steps` | `state` `'waiting' \| 'sent' \| 'confirmed' \| 'failed' \| 'cancelling' \| 'cancelled' \| 'timed-out'`; `failedReason?` (plain words → "Failed: {reason}"); `labels?` to rename the three steps. Current step has `aria-current="step"`. `commandStepState(command.status)` maps backend command statuses to a state. |
| `Timeline` | `items: { id, time (already formatted, e.g. lib/time `dayTime()`), dateTime?, title, description?, icon?, tone?, technical? }[]`, `label?`. |
| `Disclosure` | `summary`, `children`, `defaultOpen?`, `technical?` (adds `data-technical`). |
| `TechnicalDetails` | `children` (codes, MAC, IP, serial, ids, raw text), `summary` (default "Technical details"), `copyText?` (what Copy copies; default the box text), `defaultOpen?`. `<details data-technical>`, closed by default, open when the "Show technical details" preference is on; Copy → "Copied" for 1.5 s. The plain-words guard skips `[data-technical]`. |
| `TechnicalDetailsProvider` | `show: boolean`. Wrap the app with it (the preferences module passes `showTechnical`). `useShowTechnical()` reads it, e.g. to add one code line per row in long lists. |

## Overlays

All overlays portal into `#overlay-root`, make `#root` inert and hidden, lock the workspace scroll and give it
back, trap Tab, and only the innermost layer handles Escape (shared stack in `modalLock.ts`, hooks
`useDismiss` and `useFocusTrap`). Footers stay above the phone keyboard (`useKeyboardInset` →
`--keyboard-inset`).

| Component | Props |
|---|---|
| `Dialog` | `titleId`, `title`, `description?`, `onClose`, `children` (use `.dialog-body` / `.dialog-actions` as today), `className?`, `returnFocusRef?`; `size` `'sm' \| 'md' \| 'lg'` (440 / 640 / 840 px, default md); `dismissOnBackdrop` (default **false**); `dirty` (Escape / Close ask "Discard your changes?" · [Keep editing] [Discard]); `initialFocus` `'close'` (default) `\| 'first-field' \| ref`. Close label "Close". Full screen on phones. Class names `.dialog-backdrop` / `.dialog-panel` kept for e2e. `App.tsx` re-exports it as `Dialog`. |
| `Sheet` | `title`, `titleId?`, `description?`, `onClose`, `children`, `footer?` (sticky), `headerExtra?` (status line, tabs under the title), `className?`; `dismissOnBackdrop` (default **true**, ignored while dirty); `dirty`; `initialFocus`; `returnFocusRef`; `placement` `'auto' (default) \| 'side' \| 'bottom'`. Side panel from 761 px (840 px wide, clip-path reveal), bottom sheet with handle up to 760 px (92dvh, swipe the handle down to close). `data-placement` says which. |
| `ToastProvider` | Wraps the app once (`App.tsx` does). |
| `useToast()` | Returns `{ toast, notice(text), error(text), dismiss(id?), show({ tone: 'success' \| 'info' \| 'error', text, action?: { label, onClick }, duration? }), items }`. Up to 3 stacked, newest at the bottom; success / info close after 5 s (paused while hovered or focused); errors stay until closed. `toast` is the newest in the old `{ kind, text }` shape. Outside a provider it keeps its own queue (old test harnesses). Toasts are for actions the person started, never for background refresh failures. `App.tsx`'s `useToast` returns the old type (`LegacyToastApi`) so existing prop types and test doubles still fit; import from `ui` to use `show()`. |

## Lists and filters

| Component | Props |
|---|---|
| `FilterBar` | Controlled (keep values in the URL with `useUrlState`). `search?: { value, onChange, label?, placeholder? }` (× clears, "/" focuses); `chips?: { id, label, count? }[]` + `chipValue` (string or string[]) + `onChipChange(id)` + `chipsLabel`; `moreFilters?: ReactNode` ("More filters": bottom sheet on phones, popover on desktop) + `moreFiltersCount`; `active?: { id, label, onRemove }[]` (chips with ×) + `onClearAll`; `result?: { shown, total }` ("Showing 6 of 20", polite); `sort?: { value, options: { id, label }[], onChange, label? }`; `actions?`. Never use URL keys matching `cnic|password|reason|confirmation`. |
| `DataList` | `rows`, `columns: { id, header, cell(row), primary?, card? 'chips' \| 'reason' \| 'meta' \| 'hidden', align?, hideHeader?, className? }[]`, `getKey`, `label` (caption); `getHref?(row)` (whole row is one real link in the name cell); `onRowClick?(row, event)` (in-app routing: `preventDefault()`; or the action when there is no href); `rowLabel?(row)` ("{name}, {status}, last heard {ago}"); `groupBy?(row) → { id, label }` (sticky "Life (6)" headings, order kept); `changedKeys?` (rows get `data-changed` and a 1.2 s highlight); `empty?`; `layout` `'auto' \| 'table' \| 'cards'`. Table from 761 px, cards up to 760 px (name, chips line, reason line, small meta). |

## Hooks and helpers

`useFocusTrap(ref, { initialFocus, returnFocusRef, isTop })`, `useDismiss(onEscape, { enabled })` (returns
`isTop()`), `useMediaQuery(query)`, `useIsPhone()`, `useKeyboardInset(enabled)`, `lockApplicationForModal()`
(returns the release function), `formatCount(n)` (en-PK), `UI_TEXT`.

`Icon` (in `src/Icon.tsx`) has a `size` prop `'sm' | 'md' | 'lg'` (16 / 20 / 24, default 20) and the menu icons
`home`, `settings`, `update`, `tool`, `chevronDown`, `chevronLeft`, `external`, `filter`, `eye`, `eyeOff`,
`calendar`, `sort`.
