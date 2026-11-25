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
