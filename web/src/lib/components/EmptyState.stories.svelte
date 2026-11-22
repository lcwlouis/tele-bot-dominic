<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Button from './Button.svelte';
  import EmptyState from './EmptyState.svelte';

  const { Story } = defineMeta({
    title: 'Components/Empty state',
    component: EmptyState,
    tags: ['autodocs'],
    args: {
      title: "Nothing's waiting for you.",
      body: 'Soul edits, new groups and plugin drafts show up here when Dom needs a yes.',
    },
  });
</script>

<Story name="Default" />
<Story name="With an action" asChild>
  <EmptyState title="There's no page here." body="The link may be old. Home has everything that's current.">
    {#snippet action()}<Button variant="primary" href="#/">Go to Home</Button>{/snippet}
  </EmptyState>
</Story>
