import { tick } from "svelte";

// Links from outside the page (a Telegram notice, a search hit) name one item by a query parameter. A page whose
// items are not in a strip list selects the item in place: scrolled to the middle of the screen, outlined, and
// focused, so a keyboard or screen reader starts there.

/** The numeric ID a link names in `?name=`, or null. */
export function linkedId(name: string): number | null {
  const value = new URLSearchParams(location.search).get(name);
  return value !== null && /^\d+$/.test(value) ? Number(value) : null;
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
