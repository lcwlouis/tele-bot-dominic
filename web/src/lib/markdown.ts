// Markdown for the dashboard's previews. marked only tokenises; Svelte renders the tokens as elements and text, so
// no HTML string ever reaches the page: raw HTML in a note shows as its literal text, links keep only http(s) and
// mailto targets, and nothing carries a style attribute (the CSP is self-only).
import { Lexer, type Token, type TokensList } from "marked";

export type { Token };

/** The block tokens of `text`, GitHub-flavoured (tables, task lists, autolinks). */
export function tokens(text: string): TokensList {
  return new Lexer({ gfm: true }).lex(text);
}

const SAFE = new Set(["http:", "https:", "mailto:"]);

/** `href` when it is an absolute http(s) or mailto address; anything else (javascript:, data:, relative) is null. */
export function safeHref(href: string | null | undefined): string | null {
  if (!href) return null;
  try {
    return SAFE.has(new URL(href).protocol) ? href : null;
  } catch {
    return null;
  }
}

// marked leaves character references (&amp; &copy; &#39;) in text as written. A textarea's content is RCDATA, so
// setting it parses entities and never elements: what comes back is plain text, which Svelte escapes again.
let decoder: HTMLTextAreaElement | null = null;
export function decode(text: string): string {
  if (!text.includes("&")) return text;
  decoder ??= document.createElement("textarea");
  decoder.innerHTML = text;
  return decoder.value;
}
