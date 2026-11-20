// The Division of the list page. useDivision() reads "All" on /devices/:id,
// but the device panel opens over the list, which must keep its choice from
// the address while the panel is open. (Integrator: fold into lib/useDivision.)
import { useLocation } from 'react-router-dom'
import { parseDivision, type DivisionChoice } from '../../lib/division'
import { useDivision } from '../../lib/useDivision'

export function usePageDivision(): DivisionChoice {
  const [choice] = useDivision()
  const location = useLocation()
  return /^\/devices\/[^/]+/.test(location.pathname) ? parseDivision(location.search) : choice
}
