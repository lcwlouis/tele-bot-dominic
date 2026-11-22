import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// Dom's dashboard CSP: everything same-origin, no inline scripts or styles.
// `vite preview` serves the build under the same header so the screenshot tests catch any violation.
export const DOM_CSP =
  "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'";

export default defineConfig({
  plugins: [svelte()],
  // Relative asset URLs, so Dom can mount the built app under any path.
  base: './',
  build: {
    target: 'es2022',
    // Inlined assets become data: URLs, which the CSP blocks for fonts and images.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
  },
  preview: {
    port: 4173,
    strictPort: true,
    headers: { 'Content-Security-Policy': DOM_CSP },
  },
});
