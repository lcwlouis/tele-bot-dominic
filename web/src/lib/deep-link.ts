import { tick } from "svelte";

// Links from outside the page (a Telegram notice, a search hit) name one item by a query parameter. A page whose
// items are not in a strip list selects the item in place: scrolled to the middle of the screen, outlined, and
// focused, so a keyboard or screen reader starts there.

/** A row ID from a link: a positive safe integer, or 0 when the text can't be one (the API would refuse it). */
export function parseId(value: string): number {
  const id = /^\d{1,16}$/.test(value) ? Number(value) : 0;
  return Number.isSafeInteger(id) ? id : 0;
}

/**
 * What a link names in `?name=`: null with no such parameter, 0 when it can't name a row (so the page says "gone"
 * without asking the API), or the ID.
 */
export function linkedId(name: string, params = new URLSearchParams(location.search)): number | null {
  const value = params.get(name);
  return value === null ? null : parseId(value);
}

/** Bring the element with this id into view and focus it, once the page has drawn it. */
export async function reveal(id: string): Promise<void> {
  await tick();
  const element = document.getElementById(id);
  if (!element) return;
  element.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  element.focus({ preventScroll: true });
}

/** The outline a linked item wears: a ring in the signal colour, from the tokens. */
export const LINKED = "rounded-xl outline-none ring-2 ring-signal/70 ring-offset-2 ring-offset-canvas";
