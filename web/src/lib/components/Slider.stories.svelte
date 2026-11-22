<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Slider from './Slider.svelte';

  const { Story } = defineMeta({
    title: 'Components/Slider',
    component: Slider,
    tags: ['autodocs'],
    args: { label: 'Sarcasm', value: 0.5, custom: false },
    argTypes: { value: { control: { type: 'range', min: 0, max: 1, step: 0.05 } } },
  });
</script>

<Story name="Inherited" />
<Story name="Set for this chat" args={{ value: 0.8, custom: true }} />
<Story name="Disabled" args={{ disabled: true }} />
