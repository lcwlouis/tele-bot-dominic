<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import TextField from './TextField.svelte';

  const { Story } = defineMeta({
    title: 'Components/Text field',
    component: TextField,
    tags: ['autodocs'],
    args: { label: 'Base URL', value: 'https://openrouter.ai/api/v1' },
  });
</script>

<Story name="Default" />
<Story name="With a hint" args={{ hint: 'Include the API version path.' }} />
<Story name="Invalid" args={{ value: 'openrouter.ai', error: 'Start with http:// or https://.' }} />
