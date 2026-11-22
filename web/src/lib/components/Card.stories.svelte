<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Button from './Button.svelte';
  import Card from './Card.svelte';
  import ListRow from './ListRow.svelte';

  const { Story } = defineMeta({ title: 'Components/Card', component: Card, tags: ['autodocs'] });
</script>

<Story name="Plain" asChild>
  <Card title="Today" meta="since midnight">
    <p>A card groups one topic. Its title is the section's heading.</p>
  </Card>
</Story>

<Story name="Waiting, with rows" asChild>
  <Card title="Waiting for you" meta="2 things" tone="waiting" flush>
    <ul>
      <ListRow title="Group approval" meta="Climbing crew, added by Juno">
        {#snippet trail()}<Button size="sm">Review</Button>{/snippet}
      </ListRow>
      <ListRow title="Plugin draft" meta="rain-check: warns the group before a rained-out match">
        {#snippet trail()}<Button size="sm">Review</Button>{/snippet}
      </ListRow>
    </ul>
  </Card>
</Story>

<Story name="With an action" asChild>
  <Card title="Chat">
    {#snippet action()}<Button variant="quiet" size="sm">Edit</Button>{/snippet}
    <p>Writes Dom's replies.</p>
  </Card>
</Story>
