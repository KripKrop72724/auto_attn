import { NEW_SCREENS_PATH, removalDateLabel } from './classic'

export function ClassicBanner() {
  return (
    <div className="classic-banner" role="note">
      <span>You are using the old screens. They will be removed on {removalDateLabel()}.</span>
      <span aria-hidden="true"> · </span>
      <a href={NEW_SCREENS_PATH}>Back to the new screens</a>
    </div>
  )
}
