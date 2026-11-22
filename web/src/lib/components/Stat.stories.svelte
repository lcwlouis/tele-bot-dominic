<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Stat from './Stat.svelte';

  const { Story } = defineMeta({
    title: 'Components/Stat',
    component: Stat,
    tags: ['autodocs'],
    args: { label: 'Replies', value: '17' },
  });
</script>

<Story name="Number" />
<Story name="Under the cap" args={{ label: 'Spend', value: '$0.31', fill: 0.31, hint: 'of a $1.00 cap' }} />
<Story name="Near the cap" args={{ label: 'Spend', value: '$0.84', fill: 0.84, hint: 'of a $1.00 cap' }} />
<Story name="Cap hit" args={{ label: 'Spend', value: '$1.00', fill: 1, hint: 'Replies paused until midnight' }} />
<Story name="With a hint" args={{ label: 'Next reminder', value: '21:00', hint: 'Water the basil' }} />
