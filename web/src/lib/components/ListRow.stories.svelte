<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Badge from './Badge.svelte';
  import Bubble from './Bubble.svelte';
  import ListRow from './ListRow.svelte';
  import Presence from './Presence.svelte';

  const { Story } = defineMeta({ title: 'Components/List row', component: ListRow, tags: ['autodocs'] });
</script>

<Story name="Variants" asChild>
  <ul class="list">
    <ListRow title="Flat 4B" meta="Asleep until 09:00 tomorrow">
      {#snippet lead()}<Presence presence="asleep" showLabel={false} />{/snippet}
    </ListRow>
    <ListRow title="Book club (we read 0 books)" meta="3 min ago">
      <Bubble>nah season two is better and i will die on this hill</Bubble>
    </ListRow>
    <ListRow title="default" meta="This Pi">
      {#snippet trail()}<Badge tone="live">running</Badge>{/snippet}
    </ListRow>
  </ul>
</Story>

<style>
  .list {
    max-width: 480px;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
  }
</style>
