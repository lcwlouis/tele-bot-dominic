<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import type { ComponentProps } from 'svelte';
  import AppShell from './AppShell.svelte';

  const { Story } = defineMeta({
    title: 'Components/Nav',
    component: AppShell,
    tags: ['autodocs'],
    parameters: { layout: 'fullscreen' },
    args: { current: 'home', presence: 'online', waiting: 3 },
  });
</script>

{#snippet template(args: Omit<ComponentProps<typeof AppShell>, 'children'>)}
  <AppShell {...args}><h1>Page content</h1></AppShell>
{/snippet}

<Story name="Phone" globals={{ viewport: { value: 'phone', isRotated: false } }} {template} />
<Story name="Desktop" globals={{ viewport: { value: 'desktop', isRotated: false } }} {template} />
<Story name="Asleep, nothing waiting" args={{ current: 'chats', presence: 'asleep', waiting: 0 }} globals={{ viewport: { value: 'phone', isRotated: false } }} {template} />
