<script lang="ts">
  import { actions, data } from './lib/api/store.svelte';
  import AppShell from './lib/components/AppShell.svelte';
  import Button from './lib/components/Button.svelte';
  import EmptyState from './lib/components/EmptyState.svelte';
  import { router } from './lib/router.svelte';
  import Chats from './pages/Chats.svelte';
  import Home from './pages/Home.svelte';
  import Settings from './pages/Settings.svelte';

  const TITLE = { home: 'Home', chats: 'Chats', settings: 'Settings' } as const;

  $effect(() => {
    document.title = router.route ? `${TITLE[router.route]}, Dom` : 'Not found, Dom';
  });
</script>

<AppShell current={router.route} presence={data.home.dom.presence} waiting={data.home.approvals.length}>
  {#if router.route === 'home'}
    <Home data={data.home} />
  {:else if router.route === 'chats'}
    <Chats data={data.chats} {actions} />
  {:else if router.route === 'settings'}
    <Settings data={data.settings} {actions} />
  {:else}
    <div class="missing">
      <h1 class="sr-only">Page not found</h1>
      <EmptyState title="There's no page here." body="The link may be old. Home has everything that's current.">
        {#snippet action()}<Button variant="primary" href="#/">Go to Home</Button>{/snippet}
      </EmptyState>
    </div>
  {/if}
</AppShell>

<style>
  .missing {
    padding-top: var(--space-6);
  }
</style>
