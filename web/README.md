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

The screens pin the browser to the preview's clock and to UTC, so relative times and date groups hold in every
baseline. The screens start the preview on port 8765, or reuse a server already there. With another worktree's preview
running, set `DOM_PREVIEW_PORT` to a free port so the screens test this checkout.

`npm run dev` serves the app with hot reload and proxies `/api` to a fictional Dom: start
`uv run python tests/web_preview.py` from the repo root first. The dev server does not send Dom's CSP, so check
CSP-sensitive changes with `npm run build` and `npm run test:screens`.

## Layout

- `src/app.css`: Quiet Orbit tokens for both themes, mapped onto shadcn-svelte's names. Text pairs are checked for
  WCAG AA by `tests/test_dashboard_app.py`.
- `src/shadcn-svelte.css`: shadcn-svelte's Tailwind variants for Bits UI state attributes.
- `src/lib/components/kit/`: Dom's own building blocks, the baseline list pages are rebuilt from (below).
- `src/lib/components/ui/`: shadcn-svelte components, restyled. Add more with `npx shadcn-svelte@1.8.1 add <name>`.
- `src/lib/nav.ts`: the navigation: six destinations and their tabs.
- `src/routes/`: pages. `Gallery.svelte` is the component library (`/app/gallery`, reachable only by its address),
  with a mock Work list and Turn log on fixture data from `gallery-fixtures.ts`.
- `screens/`: Playwright checks and screenshot baselines (phone 390 px, desktop 1280 px, dark and light).

## Tokens

All in `src/app.css`, for both themes; every text pair is checked for WCAG AA by `tests/test_dashboard_app.py`.

- **Surfaces**, from back to front: `canvas` (the page), `rail` (the sidebar), `surface` (cards, lists), `raised`
  (fields, hover, inset blocks), `overlay` (the details panel and sheets).
- **Chart series**: `chart-1` to `chart-5`, as `fill-chart-*`, for categories in a chart (the Spend page's areas). They are
  checked for colour-blind separation against each theme's surface; a chart using them always has a legend and a
  table view.
- **Status**: `status-done`, `status-running`, `status-waiting`, `status-failed`, `status-held`, `status-paused`, as
  `text-status-*` and friends. Always with the status's glyph (`kit/status.ts`), so colour is never the only cue.
  `queued` borrows the paused tone and `cancelled` the muted text; each has its own glyph.
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
- **`RowMenu`**: a row's less common actions (delete) behind one ellipsis glyph, a Bits UI dropdown menu. `label`
  names the row ("Actions for desktop task 2") and is the tooltip; its items are `DropdownMenu.Item`s.
- **`SectionTabs`**: the sections inside one page (Settings: Models, Providers…; Access: Saved logins…), as the
  underlined row under the page's own tabs. Settings and Access share it.
- **`MessageBubble`** (`components/`): one message the Talk page's way, Dom's left and the owner's right at a readable
  width, with its time, the dashboard glyph and, on Dom's, an (i) with the turn that sent it. Talk and Chats → Your DM.
- **`StripList`**: a newest-first list of one-line strips (status glyph, title, optional meta, relative time,
  trailing action glyphs). Clicking a strip, or Enter or Space on it, opens its details beside the list on a wide
  screen (1024 px and up) or in a bottom sheet on a phone. Focus moves to the details' title and back to the strip on
  Esc or close. Generic over the item, for Work tasks, turns, messages and reminders. Long lists come with it:
  - **Two-line strips**: a strip with `chip` shows its status as a worded chip on a second line, beside `line` (the
    Work list: a task's short title, then its status and the first line of its result).
  - **Date groups**: Today, Yesterday, This week (from Monday), This month, then month names, each a heading over a
    list it names. They stick to the top while their strips scroll.
  - **Filter bar**: `search` (a text filter's label) and `statuses` (status chips) as the page chooses, plus a
    `toolbar` snippet for its own controls. Without `onfilter` it filters what is loaded; with it, the page gets
    the filter (debounced) and searches on the server. Esc in the field empties it before it closes the details.
  - **Loading and count**: `hasMore` and `onmore` load the next page as the end scrolls into view, with "Load more"
    as the fallback; a failed or empty page stops the loading and offers to try again. The count reads "Showing 50
    of 312" with `total`, or "50 loaded".
  - **Back to top** appears two screens down and returns focus to the first strip.
  - **Bounded rendering**: strips use `content-visibility: auto`, so the browser skips laying out and painting
    those off screen while they stay in the page (focus, find-in-page and the open strip survive). Measured on a
    phone-sized screen at 6x CPU slowdown, scrolling holds 60 fps at 400 strips; at 2,000 the p95 frame is 26 ms
    (51 ms without it). Past a few thousand, a real window would be the next step.
  - **Wide screens** (`contained`, on by default): the list is a pane from its top to the bottom of the screen
    that scrolls inside, so the page doesn't grow; the details beside it fill the same height. Turn it off for a
    short list.
  - **Phones** (`preview`, 10 by default): the page shows the newest few and "See all (312)", so what is below stays
    in reach. See all opens the whole list as its own screen with its own history entry: Back, Esc or the back
    gesture return to the page where it was. `preview={false}` shows the whole list on a page that is only it.
- **`GoneNote`**: the quiet note a page shows when a link names an item it no longer has ("That task is gone"),
  dismissed with its glyph.
- **Deep links** (`$lib/deep-link`): a page whose items sit in a strip list opens the linked item's details
  (`/app/work?task=<id>`, `?own=<id>`); any other page selects it in place, scrolled into view, outlined and
  focused (`/app/waiting?proposal=<id>`, `/app/work/reminders?id=<id>`). The API returns a linked item older than
  its first page with that page.
- **Traces** (`components/trace/`): `TraceView` lays out one turn's trace (dom/tracer.py) in the W2 order, and
  `TraceLoader` fetches it. `JsonBlock` is a folded request, response or prompt part, highlighted with
  `$lib/highlight` only once opened, with a copy button. The Talk page and the Turn log's trace page
  (`/app/system/turns/<id>`) both use them.
- **`AppPicker`**: the app's own combobox for a choice from a list, with a visible label and type-to-filter, in
  place of a native `<select>`.

### The glyph rule (E80)

Descriptive text that is always on screen, such as what a section is for or how a setting works, becomes an
`InfoGlyph` beside its heading: hover or focus shows it, a click or tap pins it open. Critical text stays as words
on screen: warnings (a provider that keeps data, a spend cap reached), errors and anything the owner must act on. A
warning that only matters once, like the ZDR recommendation, shows once at setup rather than forever.

## Sidebar

The six destinations, then Waiting for you at the foot with its count. The utility pages (Reminders, Access, the Turn
log…) are reached by the tabs under Work's and System's headers, so the sidebar doesn't repeat them.

On a phone each page mounts its own tab strip. It opens where the last strip of that destination was scrolled to,
moved only as far as needed to show the current tab, before the page paints, so it never jumps or slides.

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
