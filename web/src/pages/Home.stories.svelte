<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import * as fixtures from '../lib/api/fixtures';
  import type { HomeResponse } from '../lib/api/types';
  import AppShell from '../lib/components/AppShell.svelte';
  import Home from './Home.svelte';

  const QUIET: HomeResponse = {
    ...fixtures.home,
    dom: { ...fixtures.home.dom, presence: 'asleep', online_in: [] },
    vibe: { mode: 'locked', preset: { name: 'classic', title: 'Classic', description: '' }, reason: '', source: '', since: null },
    approvals: [],
    recent: [],
    today: { received: 0, replies: 0, spend_usd: 0, cap_usd: 1, next_reminder: null },
    desktop: { enabled: false, containers: [] },
  };

  const { Story } = defineMeta({
    title: 'Pages/Home',
    component: Home,
    parameters: { layout: 'fullscreen' },
    args: { data: fixtures.home },
  });
</script>

{#snippet template(args: { data: HomeResponse })}
  <AppShell current="home" presence={args.data.dom.presence} waiting={args.data.approvals.length}>
    <Home data={args.data} />
  </AppShell>
{/snippet}

<Story name="Phone" globals={{ viewport: { value: 'phone', isRotated: false } }} {template} />
<Story name="Desktop" globals={{ viewport: { value: 'desktop', isRotated: false } }} {template} />
<Story name="Quiet night, phone" args={{ data: QUIET }} globals={{ viewport: { value: 'phone', isRotated: false } }} {template} />
<Story name="Quiet night, desktop" args={{ data: QUIET }} globals={{ viewport: { value: 'desktop', isRotated: false } }} {template} />
