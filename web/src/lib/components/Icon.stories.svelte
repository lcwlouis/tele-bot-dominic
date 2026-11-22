<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Icon from './Icon.svelte';

  const NAMES = ['home', 'chats', 'settings', 'chevron', 'close', 'check', 'alert', 'moon', 'screen'] as const;

  const { Story } = defineMeta({ title: 'Components/Icon', component: Icon, tags: ['autodocs'] });
</script>

<Story name="All icons" asChild>
  <ul class="grid">
    {#each NAMES as name (name)}
      <li><Icon {name} /><span>{name}</span></li>
    {/each}
  </ul>
</Story>

<style>
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
    gap: 16px;
  }

  li {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    font-size: 0.8rem;
  }
</style>
