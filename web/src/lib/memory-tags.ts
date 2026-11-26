import { getContext, setContext } from "svelte";

// Note lines carry metadata groups (docs/memory-format.md): `{any}`, `{chat <id>}`, `{core owner DATE}`,
// `{core dom DATE: why}` and `{entities person:ID|place:slug|…}`. The Memory page's preview hides them in the text and
// shows each as a small chip, naming the chat a fact came from and what it is about. Other markdown (briefs, plugin
// panels) never sets the context, so it shows text as is.

export type EntityKind = "person" | "place" | "project" | "goal";
export type Tag =
  | { kind: "any" }
  | { kind: "chat"; id: string; name: string }
  | { kind: "core"; by: "owner" | "dom"; day: string; why: string }
  | { kind: "entity"; of: EntityKind; key: string; name: string };
export type Part = { text: string } | { tag: Tag };
/** What the chips name: chat titles by chat ID, and entities by `kind:key` (`person:7` reads as the person's name). */
export type TagNames = { chats: Record<string, string>; entities: Record<string, string> };

const KEY = Symbol("memory-tags");
const META =
  /[ \t]*\{(any|chat (-?\d+)|core (owner|dom) (\d{4}-\d{2}-\d{2})(?::([^{}\n]*))?|entities ([^{}\n]*))\}/g;
const REF = /^(person|place|project|goal):([a-z0-9][a-z0-9-]*)$/;

/** Let the markdown below read note tags, naming chats and entities from `names`. */
export function setMemoryTags(names: () => TagNames) {
  setContext(KEY, names);
}

/** An entity's readable name: the given name, else "person 7" or the slug as words ("take a break"). */
export function entityName(of: EntityKind, key: string, names: Record<string, string>): string {
  return names[`${of}:${key}`] ?? (of === "person" ? `person ${key}` : key.replaceAll("-", " "));
}

/** `text` split into plain runs and tags, or null when no Memory page set the context. */
export function splitTags(text: string): Part[] | null {
  const names = getContext<(() => TagNames) | undefined>(KEY);
  if (!names) return null;
  const parts: Part[] = [];
  let at = 0;
  for (const match of text.matchAll(META)) {
    if (match.index > at) parts.push({ text: text.slice(at, match.index) });
    if (match[1] === "any") parts.push({ tag: { kind: "any" } });
    else if (match[2] !== undefined) parts.push({ tag: { kind: "chat", id: match[2], name: names().chats[match[2]] ?? `chat ${match[2]}` } });
    else if (match[6] !== undefined) {
      // `|` separates refs (dom/notes.py); commas and spaces are read the same, and anything else is dropped.
      for (const ref of match[6].trim().split(/[|,\s]+/)) {
        const found = REF.exec(ref);
        if (found) parts.push({ tag: { kind: "entity", of: found[1] as EntityKind, key: found[2], name: entityName(found[1] as EntityKind, found[2], names().entities) } });
      }
    } else parts.push({ tag: { kind: "core", by: match[3] as "owner" | "dom", day: match[4], why: (match[5] ?? "").trim() } });
    at = match.index + match[0].length;
  }
  if (at < text.length) parts.push({ text: text.slice(at) });
  return parts;
}

/** A tag's chip text: "from: Weekend crew", "shareable", "core, pinned by you", "about: Sam". */
export function chipText(tag: Tag): string {
  if (tag.kind === "any") return "shareable";
  if (tag.kind === "chat") return `from: ${tag.name}`;
  if (tag.kind === "entity") return `about: ${tag.name}`;
  return tag.by === "owner" ? "core, pinned by you" : "core, pinned by Dom";
}

/** The characters a notes file counts against its size limit: its lines without their metadata groups (dom/notes.py's
 * `visible_len`), so a counter shown beside an editor matches the server's. */
export function visibleLength(text: string): number {
  return Array.from(text.split("\n").map(stripTags).join("\n")).length;
}

/** A line of a notes file without its metadata groups: what /api/memory/core reports as a core memory's text. */
export function stripTags(line: string): string {
  return line.replace(META, "").trimEnd();
}
