# Dom's new dashboard (Quiet Orbit)

A Svelte 5 app with [shadcn-svelte](https://shadcn-svelte.com) components (on Bits UI) and Tailwind 4. Dom serves it
at `/app`, next to the server-rendered dashboard at `/`, which keeps working until each page is ported.

The build is committed under `dom/web/static/app/`, so `/update` and the Raspberry Pi never run Node. CI
(`.github/workflows/dashboard-app.yml`) rebuilds it and fails when the committed files differ.

## Work on it

```sh
cd web
nvm use            # Node from .nvmrc
npm ci
npm run build      # writes ../dom/web/static/app; commit it with the sources
npm run check      # svelte-check
npm run test:screens          # Playwright against tests/web_preview.py (fictional data, real CSP)
npm run test:screens:update   # re-render the PNG baselines in screens/baseline after a visual change
```

`npm run dev` serves the app with hot reload and proxies `/api` to a fictional Dom: start
`uv run python tests/web_preview.py` from the repo root first. The dev server does not send Dom's CSP, so check
CSP-sensitive changes with `npm run build` and `npm run test:screens`.

## Layout

- `src/app.css`: Quiet Orbit tokens for both themes, mapped onto shadcn-svelte's names. Text pairs are checked for
  WCAG AA by `tests/test_dashboard_app.py`.
- `src/shadcn-svelte.css`: shadcn-svelte's Tailwind variants for Bits UI state attributes.
- `src/lib/components/kit/`: Dom's own building blocks, the baseline list pages are rebuilt from (below).
- `src/lib/components/ui/`: shadcn-svelte components, restyled. Add more with `npx shadcn-svelte@1.8.1 add <name>`.
- `src/lib/nav.ts`: the navigation: five destinations, their tabs, and the desktop's Workspace shortcuts.
- `src/routes/`: pages. `Gallery.svelte` is the component library (`/app/gallery`, reachable only by its address),
  with a mock Work list and Turn log on fixture data from `gallery-fixtures.ts`.
- `screens/`: Playwright checks and screenshot baselines (phone 390 px, desktop 1280 px, dark and light).

## Tokens

All in `src/app.css`, for both themes; every text pair is checked for WCAG AA by `tests/test_dashboard_app.py`.

- **Surfaces**, from back to front: `canvas` (the page), `rail` (the sidebar), `surface` (cards, lists), `raised`
  (fields, hover, inset blocks), `overlay` (the details panel and sheets).
- **Status**: `status-done`, `status-running`, `status-waiting`, `status-failed`, `status-held`, `status-paused`, as
  `text-status-*` and friends. Always with the status's glyph (`kit/status.ts`), so colour is never the only cue.
- **Type scale**: `text-caption`, `text-meta`, `text-body`, `text-title`, `text-heading`, `text-display`, each with
  its line height. New components use these; older pages move over as they are ported.
- **Spacing**: Tailwind's steps 1, 2, 3, 4, 6, 8 and 12 (4 to 48 px). Avoid one-off values in new components.
- **Motion**: `--motion-quick` (120 ms), `--motion-calm` (220 ms), `--motion-slow` (360 ms) on `ease-calm`, as
  `duration-(--motion-calm)`. Under `prefers-reduced-motion` they are all 0 ms. Motion answers the owner (opening,
  closing); nothing moves on its own except a running status's slow turn, which stops under reduced motion.

## Building blocks (`kit/`)

- **`IconButton`**: every icon-only button. `label` is required; it is the accessible name and the tooltip shown on
  hover and keyboard focus. `shortcut` adds a key hint.
- **`InfoGlyph`**: the glyph rule, below.
- **`StatusGlyph`**: a status as its glyph, with its word for screen readers (or on screen with `word`).
- **`StripList`**: a newest-first list of one-line strips (status glyph, title, optional meta, relative time,
  trailing action glyphs). Clicking a strip, or Enter or Space on it, opens its details beside the list on a wide
  screen (1024 px and up) or in a bottom sheet on a phone. Focus moves to the details' title and back to the strip on
  Esc or close. Give it `hasMore` and `onmore` and it loads the next page as the end scrolls into view, with a
  "Load more" button as the fallback. Generic over the item, for Work tasks, turns, messages and reminders.
- **`AppPicker`**: the app's own combobox for a choice from a list, with a visible label and type-to-filter, in
  place of a native `<select>`.

### The glyph rule (E80)

Descriptive text that is always on screen, such as what a section is for or how a setting works, becomes an
`InfoGlyph` beside its heading: hover or focus shows it, a click or tap pins it open. Critical text stays as words
on screen: warnings (a provider that keeps data, a spend cap reached), errors and anything the owner must act on. A
warning that only matters once, like the ZDR recommendation, shows once at setup rather than forever.

## Sidebar

On a desktop the sidebar folds to a rail (the logo spot's button, or `[`). A resting mouse opens the folded rail for a
look, over the page, which keeps its width; moving away folds it again. The logo spot is the fold button: the orbit
at rest, the panel glyph on hover or keyboard focus. On a peek, clicking it pins the sidebar open. Touch has no hover,
so it keeps tap behaviour; keyboards get each rail icon's tooltip on focus.

## Dom's CSP

Dom sends `default-src 'self'` with no exceptions: no inline scripts, inline style attributes, `<style>` elements
or other hosts. What keeps the app inside it:

- Vite emits one external script and one external stylesheet; component `<style>` blocks are compiled into that
  stylesheet. `assetsInlineLimit: 0` keeps fonts and images as files, never `data:` URLs.
- Fonts (Geist, Geist Mono, OFL) are bundled from the `@fontsource-variable/geist` packages; no CDN.
- Svelte sets dynamic styles (`style={...}`, `style:prop`) through the CSSOM (`element.style`), which CSP does not
  govern. Bits UI positions popovers, tooltips and selects the same way. A static `style="..."` attribute in a
  template, by contrast, is parsed as HTML and blocked, so none of ours use one; draw sizes with SVG attributes or
  classes instead.
- Bits UI's Command component has static `style="display: contents"` wrappers: avoid it, or wrap it, before use.
- Playwright fails on any CSP violation or request to another host, on every screen.
