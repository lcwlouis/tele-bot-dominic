import { fileURLToPath } from "node:url";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";
import { PREVIEW_PORT, PREVIEW_TOKEN } from "./screens/preview";

// The built app is committed under dom/web/static/app so `/update` and the Pi never run Node.
// Dom serves it at /app; its files at /static/app/.
export default defineConfig({
  base: "/static/app/",
  plugins: [tailwindcss(), svelte()],
  resolve: {
    alias: { $lib: fileURLToPath(new URL("./src/lib", import.meta.url)) },
  },
  build: {
    outDir: "../dom/web/static/app",
    emptyOutDir: true,
    // No inline scripts, styles or data: URLs: Dom's CSP allows 'self' only.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    cssCodeSplit: false,
    reportCompressedSize: false,
  },
  server: {
    // `npm run dev` proxies the API to tests/web_preview.py (see web/README.md).
    proxy: {
      "/api": { target: `http://127.0.0.1:${PREVIEW_PORT}`, headers: { Authorization: "Bearer " + PREVIEW_TOKEN } },
    },
  },
});
