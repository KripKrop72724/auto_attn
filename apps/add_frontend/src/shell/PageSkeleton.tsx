// The shape of a page while its code downloads (01-foundation H, FND-11):
// a title bar, four tile shapes and six rows (2×2 and four rows on phones),
// shown only after 180 ms so a fast page never flashes it.
import { useEffect, useState } from 'react'
import { Skeleton } from '../ui/Skeleton'
import { useIsPhone } from '../ui/useMediaQuery'

export function PageSkeleton({ title }: { title: string }) {
  const phone = useIsPhone()
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    const timer = window.setTimeout(() => setVisible(true), 180)
    return () => window.clearTimeout(timer)
  }, [])
  if (!visible) return null
  return (
    <div className="page-skeleton" role="status">
      <span className="sr-only">{`Loading ${title}…`}</span>
      <Skeleton variant="line" width="220px" className="page-skeleton-title" />
      <div className="page-skeleton-tiles"><Skeleton variant="tile" count={4} /></div>
      <div className="page-skeleton-rows"><Skeleton variant="row" count={phone ? 4 : 6} /></div>
    </div>
  )
}
