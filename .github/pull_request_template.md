## What changes for people

<!-- Three plain lines: what someone using the dashboard will notice. -->

## Build steps

<!-- For example: HOME-9, HOME-10 -->

## Pages touched

<!-- For example: /home, /devices -->

## Gates

- [ ] Unit tests (`pnpm test`)
- [ ] Build and bundle budget (`pnpm run build`, `pnpm run budget`; headroom table below)
- [ ] Playwright on every project, including `phone-390-touch`
- [ ] axe: no critical or serious problems (WCAG 2.2 AA tags)
- [ ] Words guard strict on the pages touched:
- [ ] No Content Security Policy warnings
- [ ] Storage and URL privacy checks (only `add.prefs` in the browser; no CNIC, password, reason or confirmation in a URL)

## Screenshots

<!-- add-ui-screenshots artifact: list the files of the changed screens, e.g. home-phone-390-touch.png, home-desktop-1440.png -->

## Bundle sizes

<!-- Paste the budget table, before → after. -->

## Safety

- One-click actions unchanged (reconcile, firmware campaigns, identity release): yes / no
- Attendance custody, delivery, recovery or release logic touched: no
- Database migration: no

## Rollback

Redeploy the previous live commit with "Deploy ADD" (`deploy_sha`), then stop merges and open a revert PR on main
(green CI and the owner's OK) before the next window. Report "main reverted: yes/no".

## Live check after deploy

<!-- The standard read-only live check (docs/add-frontend-operations.md, "Releasing a dashboard change"), plus anything this PR needs. -->
