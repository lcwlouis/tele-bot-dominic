import { getContext, setContext } from "svelte";

// Note lines carry metadata groups (docs/memory-format.md): `{any}`, `{chat <id>}`, `{core owner DATE}` and
// `{core dom DATE: why}`. The Memory page's preview hides them in the text and shows each as a small chip, naming
// the chat a fact came from. Other markdown (briefs, plugin panels) never sets the context, so it shows text as is.

export type Tag = { kind: "any" } | { kind: "chat"; id: string; name: string } | { kind: "core"; by: "owner" | "dom"; day: string; why: string };
export type Part = { text: string } | { tag: Tag };

const KEY = Symbol("memory-tags");
const META = /[ \t]*\{(any|chat (-?\d+)|core (owner|dom) (\d{4}-\d{2}-\d{2})(?::([^{}\n]*))?)\}/g;

/** Let the markdown below read note tags, naming chats from `names` (chat ID to title). */
export function setMemoryTags(names: () => Record<string, string>) {
  setContext(KEY, names);
}

/** `text` split into plain runs and tags, or null when no Memory page set the context. */
export function splitTags(text: string): Part[] | null {
  const names = getContext<(() => Record<string, string>) | undefined>(KEY);
  if (!names) return null;
  const parts: Part[] = [];
  let at = 0;
  for (const match of text.matchAll(META)) {
    if (match.index > at) parts.push({ text: text.slice(at, match.index) });
    if (match[1] === "any") parts.push({ tag: { kind: "any" } });
    else if (match[2] !== undefined) parts.push({ tag: { kind: "chat", id: match[2], name: names()[match[2]] ?? `chat ${match[2]}` } });
    else parts.push({ tag: { kind: "core", by: match[3] as "owner" | "dom", day: match[4], why: (match[5] ?? "").trim() } });
    at = match.index + match[0].length;
  }
  if (at < text.length) parts.push({ text: text.slice(at) });
  return parts;
}

/** A tag's chip text: "from: Weekend crew", "shareable", "core, pinned by you". */
export function chipText(tag: Tag): string {
  if (tag.kind === "any") return "shareable";
  if (tag.kind === "chat") return `from: ${tag.name}`;
  return tag.by === "owner" ? "core, pinned by you" : "core, pinned by Dom";
}

/** A line of a notes file without its metadata groups: what /api/memory/core reports as a core memory's text. */
export function stripTags(line: string): string {
  return line.replace(META, "").trimEnd();
}
