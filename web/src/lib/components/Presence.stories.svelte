<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Presence from './Presence.svelte';

  const { Story } = defineMeta({
    title: 'Components/Presence',
    component: Presence,
    tags: ['autodocs'],
    args: { presence: 'online', showLabel: true },
    argTypes: { presence: { control: 'inline-radio', options: ['online', 'offline', 'asleep'] } },
  });
</script>

<Story name="Online" />
<Story name="Offline" args={{ presence: 'offline' }} />
<Story name="Asleep" args={{ presence: 'asleep' }} />
<Story name="Mark only" args={{ showLabel: false }} />
