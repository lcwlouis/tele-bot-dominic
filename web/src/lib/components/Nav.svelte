<script lang="ts">
  import type { Presence as PresenceState } from '../api/types';
  import { ROUTES, type RouteId } from '../router.svelte';
  import Icon from './Icon.svelte';
  import Presence from './Presence.svelte';

  /*
    Dom's name and the main links. On phones the name sits at the top and the links are a tab bar
    stuck to the bottom (thumb reach); from 960 px both live in a left rail. AppShell places it:
    on phones the two parts land in its `brand` and `tabbar` areas, on wide screens in `rail`.
  */
  interface Props {
    current: RouteId | null;
    /** Dom's overall presence, shown beside his name. */
    presence?: PresenceState;
    /** Things waiting for the owner; shown on Home's link. */
    waiting?: number;
  }

  let { current, presence, waiting = 0 }: Props = $props();
</script>

<div class="rail">
  <div class="rail-inner">
    <header class="brand">
      <a class="wordmark" href="#/">dom<span class="sr-only">'s dashboard, Home</span></a>
      {#if presence}<Presence {presence} />{/if}
    </header>

    <nav aria-label="Main">
      <ul>
        {#each ROUTES as route (route.id)}
          <li>
            <a href={route.href} aria-current={route.id === current ? 'page' : undefined}>
              <span class="icon">
                <Icon name={route.icon} />
                {#if route.id === 'home' && waiting > 0}
                  <span class="count num" aria-hidden="true">{waiting}</span>
                {/if}
              </span>
              <span>
                {route.label}{#if route.id === 'home' && waiting > 0}<span class="sr-only">, {waiting} waiting for you</span>{/if}
              </span>
            </a>
          </li>
        {/each}
      </ul>
    </nav>
  </div>
</div>

<style>
  /* Phones: the wrappers step aside so the name and the tab bar are placed separately. */
  .rail,
  .rail-inner {
    display: contents;
  }

  .brand {
    grid-area: brand;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-4);
  }

  .wordmark {
    display: inline-block;
    padding: 2px 14px 4px;
    border-radius: var(--radius-bubble);
    background: var(--bubble);
    color: var(--bubble-ink);
    font-family: var(--font-display);
    font-size: var(--step-1);
    font-weight: 800;
    letter-spacing: -0.03em;
    text-decoration: none;
  }

  nav {
    grid-area: tabbar;
    position: sticky;
    z-index: 10;
    bottom: 0;
    padding-bottom: env(safe-area-inset-bottom);
    background: var(--surface);
    border-top: 1px solid var(--line);
  }

  ul {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
  }

  li a {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 2px;
    height: var(--tabbar);
    color: var(--ink-muted);
    font-size: var(--step--1);
    font-weight: 700;
    text-decoration: none;
  }

  li a[aria-current='page'] {
    color: var(--grape);
  }

  .icon {
    position: relative;
    display: grid;
    place-items: center;
    width: 56px;
    height: 30px;
    border-radius: var(--radius-pill);
    transition: background-color var(--dur-fast) var(--ease-out);
  }

  li a[aria-current='page'] .icon {
    background: var(--grape-soft);
  }

  .count {
    position: absolute;
    top: -4px;
    right: 6px;
    min-width: 18px;
    height: 18px;
    padding: 0 5px;
    border-radius: var(--radius-pill);
    background: var(--mango-fill);
    color: var(--fill-ink);
    font-size: 11px;
    font-weight: 700;
    line-height: 18px;
    text-align: center;
  }

  @media (min-width: 960px) {
    .rail {
      grid-area: rail;
      display: block;
      background: var(--surface);
      border-right: 1px solid var(--line);
    }

    .rail-inner {
      position: sticky;
      top: 0;
      display: flex;
      flex-direction: column;
      gap: var(--space-5);
      padding: var(--space-5) var(--space-3);
    }

    .brand {
      flex-direction: column;
      align-items: flex-start;
      padding: 0 var(--space-3);
    }

    nav {
      position: static;
      padding: 0;
      background: none;
      border: 0;
    }

    ul {
      grid-template-columns: 1fr;
      gap: var(--space-1);
    }

    li a {
      flex-direction: row;
      justify-content: flex-start;
      gap: var(--space-2);
      height: var(--hit);
      padding-right: var(--space-3);
      font-size: var(--step-0);
      border-radius: var(--radius-pill);
    }

    li a:hover {
      color: var(--ink);
    }

    li a[aria-current='page'] {
      background: var(--grape-soft);
    }

    li a[aria-current='page'] .icon {
      background: transparent;
    }

    .icon {
      width: 44px;
    }
  }
</style>
