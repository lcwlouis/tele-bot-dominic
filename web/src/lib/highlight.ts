// Syntax highlighting for code the dashboard shows. highlight.js marks tokens with classes only (no inline styles,
// which the CSP forbids) and escapes the text itself; the colours come from Quiet Orbit tokens in app.css.
// Only the core and the languages Dom's code, plugins, skills and notes use are bundled.
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import ini from "highlight.js/lib/languages/ini";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

const LANGUAGES = { bash, css, diff, ini, javascript, json, markdown, python, sql, typescript, xml, yaml };
for (const [name, language] of Object.entries(LANGUAGES)) hljs.registerLanguage(name, language);

// File extensions and fence words that name one of the bundled languages.
const ALIASES: Record<string, keyof typeof LANGUAGES> = {
  py: "python", python: "python",
  js: "javascript", mjs: "javascript", cjs: "javascript", javascript: "javascript",
  ts: "typescript", typescript: "typescript", svelte: "xml",
  json: "json", jsonc: "json",
  sh: "bash", bash: "bash", shell: "bash", zsh: "bash", console: "bash",
  md: "markdown", markdown: "markdown",
  yml: "yaml", yaml: "yaml",
  toml: "ini", ini: "ini", cfg: "ini", env: "ini",
  html: "xml", xml: "xml", svg: "xml",
  css: "css", sql: "sql", diff: "diff", patch: "diff",
};

/** The bundled language for a fence word or a file path's extension, or null when it is not one we highlight. */
export function languageOf(nameOrPath: string | null | undefined): string | null {
  if (!nameOrPath) return null;
  const word = nameOrPath.toLowerCase();
  const extension = word.includes(".") ? word.slice(word.lastIndexOf(".") + 1) : word;
  return ALIASES[extension] ?? null;
}

/** Escaped, class-marked HTML for `text` in `language`, or null when the language is not bundled. */
export function highlight(text: string, language: string | null): string | null {
  if (!language || !hljs.getLanguage(language)) return null;
  return hljs.highlight(text, { language, ignoreIllegals: true }).value;
}
