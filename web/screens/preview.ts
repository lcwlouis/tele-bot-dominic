// tests/web_preview.py: a fictional Dom on the real dashboard app, for the screens and `npm run dev`.
// The token is a fixed local test value, accepted only by that preview server.
// DOM_PREVIEW_PORT picks another port, so screens in two worktrees don't reuse each other's server.
export const PREVIEW_PORT = Number(process.env.DOM_PREVIEW_PORT ?? 8765);
export const PREVIEW_TOKEN = "preview-token-" + "p".repeat(32);
