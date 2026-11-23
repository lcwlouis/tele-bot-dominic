/** A path router for /app: links to /app/... change the page in place; everything else is a normal link. */
export const BASE = "/app";

class Route {
  path = $state(normalise(location.pathname));
  search = $state(location.search);
}

function normalise(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  return trimmed === "" ? BASE : trimmed;
}

export const route = new Route();

export function navigate(href: string) {
  const url = new URL(href, location.href);
  history.pushState({}, "", url);
  route.path = normalise(url.pathname);
  route.search = url.search;
  window.scrollTo({ top: 0 });
  // Move focus to the new page's heading so keyboard and screen-reader users land on it.
  queueMicrotask(() => document.querySelector<HTMLElement>("main h1")?.focus());
}

export function isAppLink(anchor: HTMLAnchorElement): boolean {
  const url = new URL(anchor.href, location.href);
  return url.origin === location.origin && (url.pathname === BASE || url.pathname.startsWith(BASE + "/"));
}

export function interceptLinks(event: MouseEvent) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
    return;
  }
  const anchor = (event.target as Element | null)?.closest?.("a");
  if (anchor && !anchor.getAttribute("href")?.startsWith("#") && !anchor.target && !anchor.hasAttribute("download") && isAppLink(anchor)) {
    event.preventDefault();
    navigate(anchor.href);
  }
}

addEventListener("popstate", () => {
  route.path = normalise(location.pathname);
  route.search = location.search;
});
