<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import ErrorState from './ErrorState.svelte';

  const { Story } = defineMeta({
    title: 'Components/Error state',
    component: ErrorState,
    tags: ['autodocs'],
    args: {
      title: "Couldn't load your chats.",
      body: "Dom didn't answer. Check he's running on the Pi, then try again.",
    },
  });
</script>

<Story name="Default" />
<Story name="With retry" args={{ onretry: () => {} }} />
