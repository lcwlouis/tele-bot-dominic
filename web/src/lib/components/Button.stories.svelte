<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import type { ComponentProps } from 'svelte';
  import Button from './Button.svelte';

  const { Story } = defineMeta({
    title: 'Components/Button',
    component: Button,
    tags: ['autodocs'],
    args: { variant: 'secondary', size: 'md', busy: false, disabled: false },
    argTypes: {
      variant: { control: 'inline-radio', options: ['primary', 'secondary', 'quiet', 'danger'] },
      size: { control: 'inline-radio', options: ['md', 'sm'] },
    },
  });

  type Args = Omit<ComponentProps<typeof Button>, 'children'>;
</script>

{#snippet template(args: Args)}
  <Button {...args}>Save changes</Button>
{/snippet}

<Story name="Primary" args={{ variant: 'primary' }} {template} />
<Story name="Secondary" {template} />
<Story name="Quiet" args={{ variant: 'quiet' }} {template} />
<Story name="Danger" args={{ variant: 'danger' }}>
  {#snippet template(args: Args)}<Button {...args}>Leave group</Button>{/snippet}
</Story>
<Story name="Small" args={{ size: 'sm' }}>
  {#snippet template(args: Args)}<Button {...args}>Review</Button>{/snippet}
</Story>
<Story name="Busy" args={{ variant: 'primary', busy: true }} {template} />
<Story name="Disabled" args={{ disabled: true }} {template} />
<Story name="As a link" args={{ href: '#/' }}>
  {#snippet template(args: Args)}<Button {...args}>Go to Home</Button>{/snippet}
</Story>
