import { createContext, useContext, type ReactNode } from 'react'

// The "Show technical details" preference. App.tsx provides it from add.prefs
// (lib/prefs); without a provider every box starts closed. Kept apart from
// TechnicalDetails.tsx so the frame can provide it without loading that CSS.
const TechnicalDetailsContext = createContext(false)

export function TechnicalDetailsProvider({ show, children }: { show: boolean; children: ReactNode }) {
  return <TechnicalDetailsContext.Provider value={show}>{children}</TechnicalDetailsContext.Provider>
}

export const useShowTechnical = () => useContext(TechnicalDetailsContext)
