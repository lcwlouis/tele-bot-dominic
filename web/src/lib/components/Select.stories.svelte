<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Select from './Select.svelte';

  const OPTIONS = [
    { value: '', label: 'Follow the vibe (Gremlin)' },
    { value: 'classic', label: 'Classic' },
    { value: 'assistant', label: 'Assistant' },
  ];

  const { Story } = defineMeta({
    title: 'Components/Select',
    component: Select,
    tags: ['autodocs'],
    args: { label: 'Preset', options: OPTIONS, value: '' },
  });
</script>

<Story name="Default" />
<Story name="With a hint" args={{ hint: 'Chats with their own preset keep it.' }} />
<Story name="Disabled" args={{ disabled: true }} />
