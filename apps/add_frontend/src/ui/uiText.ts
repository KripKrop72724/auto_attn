// The few fixed words the kit itself shows (glossary and writing rules,
// 01-foundation "Words"). Everything about devices, statuses and codes comes
// from fe/words through the callers; these are only the kit's own controls.
export const UI_TEXT = {
  close: 'Close',
  closeMessage: 'Close message',
  discardTitle: 'Discard your changes?',
  discardBody: 'What you typed will be lost.',
  keepEditing: 'Keep editing',
  discard: 'Discard',
  tryAgain: 'Try again',
  trying: 'Trying…',
  errorTitle: "Couldn't load this",
  errorFallback: 'Something went wrong. Try again in a minute.',
  reference: 'Reference',
  emptyTitle: 'Nothing here yet',
  technicalDetails: 'Technical details',
  copy: 'Copy',
  copied: 'Copied',
  moreActions: 'More actions',
  search: 'Search',
  clearSearch: 'Clear search',
  moreFilters: 'More filters',
  clearAll: 'Clear all',
  removeFilter: (label: string) => `Remove filter: ${label}`,
  showing: (shown: string, total: string) => `Showing ${shown} of ${total}`,
  sort: 'Sort',
  showResults: 'Show results',
  loading: 'Loading…',
  division: 'Division',
  all: 'All',
  life: 'Life',
  health: 'Health',
  showingOnly: (division: string) => `Showing ${division} only`,
  showAll: 'Show all',
  stepWaiting: 'Waiting',
  stepSent: 'Sent to machine',
  stepConfirmed: 'Confirmed',
  stepFailed: (reason?: string) => reason ? `Failed: ${reason}` : 'Failed',
  stepCancelling: 'Cancelling…',
  stepCancelled: 'Cancelled',
  stepTimedOut: 'Timed out: the machine did not answer',
  progress: 'Progress',
} as const

const numberFormat = new Intl.NumberFormat('en-PK')

// "158,560" everywhere, never the browser's own locale.
export const formatCount = (value: number) => numberFormat.format(value)
