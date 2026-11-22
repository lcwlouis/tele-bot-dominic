# Dom's dashboard (web/)

The new dashboard: a Svelte 5 single-page app built with Vite into static files that Dom serves from
his own origin. This is the baseline from track F of the v0.8.1 plan: the design system, a component
library in Storybook, and three pages (Home, Chats, Settings) built against typed fixture data. The
current dashboard in `dom/web/` keeps running until each page here replaces its old one.

The Pi never runs Node. Builds happen on a dev machine or in GitHub Actions.

## Run it

Node 24 (see `.nvmrc`). From this folder:

```sh
npm ci --ignore-scripts
npm run dev              # http://localhost:5173, hot reload, fixture data
npm run check            # svelte-check: types and Svelte warnings (warnings fail)
npm run build            # static files in dist/
npm run preview          # serves dist/ under Dom's real CSP, http://127.0.0.1:4173
npm run storybook        # component library, http://localhost:6006
npm run build-storybook  # static Storybook in storybook-static/
```

## Screenshots

`tests/screens.spec.ts` serves the build with `vite preview` (which sends Dom's exact
`Content-Security-Policy` header) and, for Home, Chats and Settings at 390 px and 1280 px in light and dark:

- compares a screenshot with the baseline in `tests/screenshots/`,
- fails on any CSP violation or console error,
- runs axe and fails on any accessibility violation,
- plus a few interaction checks (a slider change, leaving a group, a model test).

Baselines are made on Linux in the same Playwright image CI uses, so text renders identically.
Making them on a Mac gives different font rendering and CI would fail. To run or update them:

```sh
npm run build
PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright:v1.65.1-noble@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579
# compare against the baselines
docker run --rm --platform linux/amd64 -v "$PWD/..":/work -v /work/web/node_modules -w /work/web \
  "$PLAYWRIGHT_IMAGE" bash -c "npm ci --ignore-scripts && npx playwright test"
# accept a deliberate visual change
docker run --rm --platform linux/amd64 -v "$PWD/..":/work -v /work/web/node_modules -w /work/web \
  "$PLAYWRIGHT_IMAGE" bash -c "npm ci --ignore-scripts && npx playwright test --update-snapshots"
```

The second `-v` keeps the container's Linux `node_modules` away from yours. The image is pinned by digest,
the same one `.github/workflows/web.yml` uses; keep its tag equal to the Playwright test package version in
`package.json`, and when you move either, update both and remake the baselines.

`npx playwright test` on a Mac is fine for the CSP, accessibility and interaction checks; expect
the screenshot comparisons to differ.

CI (`.github/workflows/web.yml`) runs on every change under `web/`: install, type-check, build, build
Storybook, and the Playwright suite.

## Why it is built this way

- **Plain Svelte with a hash router, not SvelteKit.** Dom's CSP is `default-src 'self'` with no
  `'unsafe-inline'`. SvelteKit's SPA output starts itself with an inline `<script>`, which that CSP
  blocks; plain Vite emits only external files. Three pages need no more than `src/lib/router.svelte.ts`.
  Hash routes (`#/chats`) mean Dom serves one `index.html` and needs no route table, and the app works
  under whatever path it is mounted at (`base: './'`).
- **CSP-safe by construction.** No inline scripts or style attributes in markup, no third-party
  requests, fonts self-hosted in `src/lib/assets/fonts/` (OFL, licences beside the files), and
  `assetsInlineLimit: 0` so nothing becomes a `data:` URL. Dynamic values (the slider fill) are set
  through the CSSOM, which the CSP allows. The screenshot suite enforces this.
- **Exact versions.** `.npmrc` sets `save-exact`; `package-lock.json` is committed.

## Layout of the code

```
src/
  main.ts, App.svelte        entry and routing
  lib/api/types.ts           the JSON contract with Dom (read this first when building the API)
  lib/api/fixtures.ts        made-up data typed against it
  lib/api/store.svelte.ts    app data and every owner action: the one seam the real API replaces
  lib/components/            the design system, each with a .stories.svelte
  lib/styles/tokens.css      design tokens
  lib/styles/base.css        fonts, reset, global helpers
  lib/format.ts              times and money, read in the owner's timezone
  lib/router.svelte.ts       hash routes
  lib/toast.svelte.ts        toast queue
  pages/                     Home, Chats, Settings (+ RoleCard), each with stories at both widths
tests/                       Playwright suite and screenshot baselines
```

## The design system

Dom is a cheeky twenty-something who lives in your group chats, so the dashboard borrows from texting
rather than from admin panels. The rules:

- **Bubbles are Dom talking.** Anything in Dom's own words (a reply, his reason for today's vibe) sits in
  a grape speech bubble with one squared corner. Everything the dashboard itself says is plain text
  outside bubbles, in sentence case.
- **Colour is meaning.** Grape is Dom and things you can press. Mint is live (online, key set, test
  passed). Mango is waiting on you: a screen with no mango has nothing for you to do. Cherry is broken
  or destructive.
- **One entrance.** On Home, Dom's vibe bubble shows typing dots for a beat before the words pop in.
  Other motion only answers the owner (a row opening, a toast, a toggle). Reduced motion turns it off.
- **Thumb first.** Phone layout is the default: a tab bar at the bottom, 44 px touch targets, one column.
  From 960 px a left rail takes the tab bar's place and pages go to two columns.

Tokens (`src/lib/styles/tokens.css`), all CSS custom properties:

| Group | Tokens |
|---|---|
| Colour | `--paper`, `--surface`, `--surface-sunk`, `--line`, `--line-strong`, `--ink`, `--ink-muted`, `--grape*`, `--mint*`, `--mango*`, `--cherry*`, `--bubble*`, `--fill-ink`, `--scrim`, `--shadow-pop` |
| Type | `--font-display` (Unbounded), `--font-body` (Atkinson Hyperlegible), `--step--1` to `--step-4` (1.25 scale), `--leading-body`, `--leading-tight` |
| Space | `--space-1` to `--space-8` (4, 8, 12, 16, 24, 32, 48, 64 px) |
| Shape | `--radius-sm` (chips, fields), `--radius-md` (panels), `--radius-pill` (buttons, toggles), `--radius-bubble` (Dom) |
| Motion | `--ease-out`, `--ease-pop`, `--dur-fast`, `--dur-med`, `--dur-slow` |
| Size | `--hit` (44 px), `--tabbar`, `--nav-rail`, `--content-max` |

Each colour is `light-dark(light, dark)`: the app follows the system theme, and `data-theme="light"` or
`"dark"` on `<html>` pins one (Storybook's Theme toolbar does this).

Components: `AppShell`, `Nav`, `Card`, `ListRow`, `Stat`, `Badge`, `Bubble`, `Presence`, `Button`,
`Toggle`, `Slider`, `Select`, `TextField`, `ConfirmDialog`, `EmptyState`, `ErrorState`, `Toaster`
(with `toast()` from `lib/toast.svelte.ts`), `Icon`.

## Not wired yet

Everything runs on fixtures. Actions change the page optimistically and confirm with a toast, but
nothing reaches Dom. Next steps, one page at a time: Dom serves the GETs and actions in
`lib/api/types.ts`, `store.svelte.ts` swaps its fixture loads and `send()` for `fetch`, and Dom serves
`dist/` from the release commit (the plan has CI check the built files match the source).
"Review" and "Open" links on Home point at the current dashboard's pages until those are ported.
