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
- `src/lib/components/ui/`: shadcn-svelte components, restyled. Add more with `npx shadcn-svelte@1.8.1 add <name>`.
- `src/lib/nav.ts`: the navigation: five destinations, their tabs, and the desktop's Workspace shortcuts.
- `src/routes/`: pages. `Gallery.svelte` is the component library (`/app/gallery`).
- `screens/`: Playwright checks and screenshot baselines (phone 390 px, desktop 1280 px, dark and light).

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
