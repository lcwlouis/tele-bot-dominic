<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Toggle from './Toggle.svelte';

  const { Story } = defineMeta({
    title: 'Components/Toggle',
    component: Toggle,
    tags: ['autodocs'],
    args: {
      label: 'Let Dom edit his own memory',
      description: 'He proposes each change to his notes in your DM. Nothing changes until you approve it.',
      checked: false,
    },
  });
</script>

<Story name="Off" />
<Story name="On" args={{ checked: true }} />
<Story name="Disabled" args={{ disabled: true }} />
<Story name="Without a description" args={{ description: '' }} />
