<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import * as fixtures from '../lib/api/fixtures';
  import { noopActions } from '../lib/api/store.svelte';
  import type { SettingsResponse } from '../lib/api/types';
  import AppShell from '../lib/components/AppShell.svelte';
  import Settings from './Settings.svelte';

  const { Story } = defineMeta({
    title: 'Pages/Settings',
    component: Settings,
    parameters: { layout: 'fullscreen' },
    args: { data: fixtures.settings, actions: noopActions },
  });
</script>

{#snippet template(args: { data: SettingsResponse })}
  <AppShell current="settings" presence="online" waiting={fixtures.home.approvals.length}>
    <Settings data={args.data} actions={noopActions} />
  </AppShell>
{/snippet}

<Story name="Phone" globals={{ viewport: { value: 'phone', isRotated: false } }} {template} />
<Story name="Desktop" globals={{ viewport: { value: 'desktop', isRotated: false } }} {template} />
