<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Bubble from './Bubble.svelte';

  const { Story } = defineMeta({
    title: 'Components/Bubble',
    component: Bubble,
    tags: ['autodocs'],
    args: { size: 'sm', typing: false },
    argTypes: { size: { control: 'inline-radio', options: ['sm', 'lg'] } },
  });
</script>

<Story name="Reply">nah season two is better and i will die on this hill</Story>
<Story name="Large, with typing" args={{ size: 'lg', typing: true }}>slept like a rock, woke up choosing chaos</Story>
<Story name="Reaction">🤣</Story>
