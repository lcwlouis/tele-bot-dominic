<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { Presence } from '../api/types';
  import type { RouteId } from '../router.svelte';
  import Nav from './Nav.svelte';
  import Toaster from './Toaster.svelte';

  /*
    The page frame. Phones: Dom's name, the page, then the tab bar stuck to the bottom.
    From 960 px: a rail on the left and the page beside it. Grid areas: brand, main, tabbar, rail.
  */
  interface Props {
    current: RouteId | null;
    presence?: Presence;
    waiting?: number;
    children: Snippet;
  }

  let { current, presence, waiting = 0, children }: Props = $props();
</script>

<a class="skip" href="#main">Skip to content</a>
<div class="shell">
  <Nav {current} {presence} {waiting} />
  <main id="main" tabindex="-1">
    {@render children()}
  </main>
</div>
<Toaster />

<style>
  .skip {
    position: fixed;
    z-index: 30;
    top: var(--space-2);
    left: var(--space-2);
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-pill);
    background: var(--grape);
    color: var(--grape-ink);
    font-weight: 700;
    translate: 0 -200%;
  }

  .skip:focus-visible {
    translate: 0 0;
  }

  .shell {
    display: grid;
    grid-template-areas: 'brand' 'main' 'tabbar';
    grid-template-rows: auto 1fr auto;
    min-height: 100dvh;
  }

  main {
    grid-area: main;
    width: 100%;
    max-width: var(--content-max);
    margin: 0 auto;
    padding: var(--space-2) var(--space-4) var(--space-7);
    min-width: 0;
  }

  main:focus {
    outline: none;
  }

  @media (min-width: 960px) {
    .shell {
      grid-template-areas: 'rail main';
      grid-template-columns: var(--nav-rail) minmax(0, 1fr);
      grid-template-rows: 1fr;
    }

    main {
      padding: var(--space-6) var(--space-6) var(--space-7);
    }
  }
</style>
