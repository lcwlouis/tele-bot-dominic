<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Button from './Button.svelte';
  import ConfirmDialog from './ConfirmDialog.svelte';

  const { Story } = defineMeta({ title: 'Components/Confirm dialog', component: ConfirmDialog, tags: ['autodocs'] });
</script>

<script lang="ts">
  let leave = $state(true);
  let reset = $state(false);
</script>

<Story name="Destructive" asChild>
  <Button variant="danger" onclick={() => (leave = true)}>Leave group</Button>
  <ConfirmDialog bind:open={leave} title="Leave Sunday league?" confirmLabel="Leave group" onconfirm={() => {}}>
    Dom leaves the group and stops reading it. Someone there has to add him again to undo this.
  </ConfirmDialog>
</Story>

<Story name="Reversible" asChild>
  <Button onclick={() => (reset = true)}>Reset sliders</Button>
  <ConfirmDialog bind:open={reset} title="Reset sliders in Book club?" confirmLabel="Reset sliders" tone="primary" onconfirm={() => {}}>
    Every slider in this chat goes back to the value set everywhere.
  </ConfirmDialog>
</Story>
