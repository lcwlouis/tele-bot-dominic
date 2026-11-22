<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Badge from './Badge.svelte';

  const { Story } = defineMeta({ title: 'Components/Badge', component: Badge, tags: ['autodocs'] });
</script>

<Story name="Tones" asChild>
  <div class="row">
    <Badge>Cloud</Badge>
    <Badge tone="live">Key set</Badge>
    <Badge tone="waiting">waiting for you</Badge>
    <Badge tone="danger">Key missing</Badge>
    <Badge tone="dom">Gremlin</Badge>
  </div>
</Story>

<style>
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
</style>
