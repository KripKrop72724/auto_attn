# ADD old screens (`/classic/`), one week after the revamp

The UI/UX revamp went live as one release. The owner was promised the old
screens one click away for one week after that release, so the web image
serves two apps from the same nginx:

| URL | App | Source |
| --- | --- | --- |
| `/` and every dashboard route, `/status` | the new screens | `apps/add_frontend` |
| `/classic/…` | the old screens, frozen | `apps/add_frontend_classic` |

## What the old screens are

- A verbatim copy of `apps/add_frontend` at `main` `b83a2d75` (20 Nov 2026),
  taken before any revamp change. Only these changes were made to the copy:
  - `vite.config.ts`: `base: '/classic/'`, no test settings.
  - `src/App.tsx`: the router uses `basename: '/classic'`.
  - Plain `<a href="/…">` links, the logo and two `history.replaceState` calls
    go through `withBase()` / `LOGO_SRC` in `src/classic.ts` so they stay
    under `/classic/`. `/api/…` download links are unchanged.
  - `src/main.tsx` shows the banner (`src/ClassicBanner.tsx`, `src/classic.css`)
    above the app. The public `/status` page was removed from the copy: only
    the new app serves `/status`.
  - `index.html` is titled "ADD (old screens)", has `noindex`, and no web
    app manifest (so nobody installs the old screens as a phone app).
  - Unit tests, browser tests, the bundle budget script and the container
    files were not copied. `package.json` keeps the same dependencies and the
    same `pnpm-lock.yaml`; only its name and scripts changed.
- It talks to the same `/api` and `/events` on the same origin, so the
  sign-in cookie is shared. Signing in or out on one set of screens does the
  same on the other.
- The banner reads "You are using the old screens. They will be removed on
  {date}. · Back to the new screens". The date is the constant
  `CLASSIC_REMOVAL_DATE` in `apps/add_frontend_classic/src/classic.ts`. Set it
  to the release date plus seven days before the release is deployed.
- The new app's Settings page has the link "Use the old screens for now"
  pointing to `/classic/`.

## How it is built and served

- `apps/add_frontend/Dockerfile` has a second build stage, `build-classic`,
  which installs and builds `apps/add_frontend_classic`. The final nginx stage
  copies its `dist` to `/usr/share/nginx/html/classic`.
- `apps/add_frontend/nginx.conf` has three blocks before `location /`:
  `location = /classic` (relative redirect to `/classic/`),
  `location /classic/assets/` (long-lived cache, security headers) and
  `location /classic/` (SPA fallback to `/classic/index.html`, `no-store`,
  security headers). The Content-Security-Policy is unchanged: the old
  screens load only same-origin scripts and stylesheets.
- CI (`.github/workflows/add-ci.yml`):
  - the `frontend` job tests, builds and budget-checks `apps/add_frontend`
    only, as before;
  - the `frontend-classic` job only installs and builds the old screens;
  - the `containers` job smoke-tests `/classic/`, `/classic/fleet`, the
    `/classic` redirect and the security headers, plus `/` and `/status`.
- `scripts/check_repository_contract.py` lists `add_frontend_classic` as an
  allowed app.
- `tests/unit/test_classic_screens_contract.py` guards the wiring above.

## Removal (one PR, after `CLASSIC_REMOVAL_DATE`)

Do all of this in one PR so the rollback stays one deploy:

1. Delete the folder `apps/add_frontend_classic/`.
2. `apps/add_frontend/Dockerfile`: delete the `build-classic` stage (from the
   comment "The old screens, served under /classic/" to its `RUN pnpm run
   build`) and the line
   `COPY --from=build-classic /classic/dist /usr/share/nginx/html/classic`.
3. `apps/add_frontend/nginx.conf`: delete the comment and the three blocks
   `location = /classic`, `location /classic/assets/` and `location /classic/ {`.
   After that a stale `/classic/…` bookmark falls through to the new app,
   which treats it like any other unknown address.
4. `.github/workflows/add-ci.yml`: delete the `frontend-classic` job and the
   `/classic` lines in the `containers` smoke test (keep the `/status` line).
5. `scripts/check_repository_contract.py`: remove `"add_frontend_classic",`
   and its comment from `expected_apps`.
6. Delete `tests/unit/test_classic_screens_contract.py`.
7. `.gitignore`: remove the three `apps/add_frontend_classic/` lines.
8. New app: remove the Settings link "Use the old screens for now" and its
   test.
9. Delete this file, and check nothing else mentions `/classic`:
   `git grep -n -e classic -e "old screens"`.
