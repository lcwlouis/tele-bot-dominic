/*
  Hash routing: #/, #/chats, #/settings. Dom serves one static index.html, so the server needs no
  route table, and the app works under whatever path it is mounted at.
*/
import type { IconName } from './components/Icon.svelte';

export type RouteId = 'home' | 'chats' | 'settings';

export interface RouteInfo {
  id: RouteId;
  href: string;
  label: string;
  icon: IconName;
}

export const ROUTES: readonly RouteInfo[] = [
  { id: 'home', href: '#/', label: 'Home', icon: 'home' },
  { id: 'chats', href: '#/chats', label: 'Chats', icon: 'chats' },
  { id: 'settings', href: '#/settings', label: 'Settings', icon: 'settings' },
];

/** The route a hash points at, or null for one that doesn't exist. */
export function parse(hash: string): RouteId | null {
  const path = hash.replace(/^#/, '').split('?')[0] || '/';
  const match = ROUTES.find((route) => route.href.slice(1) === path);
  return match ? match.id : null;
}

export const router = $state<{ route: RouteId | null }>({ route: parse(window.location.hash) });

window.addEventListener('hashchange', () => {
  router.route = parse(window.location.hash);
  window.scrollTo(0, 0);
});
