import { mergeProps } from "bits-ui";
import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// The type scale's named steps (app.css) are font sizes, not colours: without this, cn("text-body text-signal")
// would drop text-body as a clashing colour.
const twMerge = extendTailwindMerge({ extend: { theme: { text: ["caption", "meta", "body", "title", "heading", "display"] } } });

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// Types the shadcn-svelte components share.
export type WithoutChild<T> = T extends { child?: any } ? Omit<T, "child"> : T;
export type WithoutChildren<T> = T extends { children?: any } ? Omit<T, "children"> : T;
export type WithoutChildrenOrChild<T> = WithoutChildren<WithoutChild<T>>;
export type WithElementRef<T, U extends HTMLElement = HTMLElement> = T & { ref?: U | null };

/**
 * Props for an element that is a tooltip's trigger: the tooltip's handlers and the element's own both run
 * (`mergeProps` composes them, where a later spread would silently replace them). When the tooltip only repeats
 * the element's accessible name, it is not also its description, or screen readers would say the name twice.
 */
export function tipTrigger(props: Record<string, unknown>, own: Record<string, unknown> = {}, repeatsName = true) {
  const merged = mergeProps(props, own);
  if (repeatsName) delete merged["aria-describedby"];
  return merged;
}
