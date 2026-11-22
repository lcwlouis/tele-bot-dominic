<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import * as fixtures from '../lib/api/fixtures';
  import { noopActions } from '../lib/api/store.svelte';
  import type { ChatsResponse } from '../lib/api/types';
  import AppShell from '../lib/components/AppShell.svelte';
  import Chats from './Chats.svelte';

  const { Story } = defineMeta({
    title: 'Pages/Chats',
    component: Chats,
    parameters: { layout: 'fullscreen' },
    args: { data: fixtures.chats, actions: noopActions, openId: 101 },
  });
</script>

{#snippet template(args: { data: ChatsResponse; openId?: number | null })}
  <AppShell current="chats" presence="online" waiting={fixtures.home.approvals.length}>
    <Chats data={args.data} actions={noopActions} openId={args.openId} />
  </AppShell>
{/snippet}

<Story name="Phone" globals={{ viewport: { value: 'phone', isRotated: false } }} {template} />
<Story name="Desktop" globals={{ viewport: { value: 'desktop', isRotated: false } }} {template} />
<Story
  name="No chats yet, phone"
  args={{ data: { ...fixtures.chats, chats: [] }, openId: null }}
  globals={{ viewport: { value: 'phone', isRotated: false } }}
  {template}
/>
