<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import { toast, toasts } from '../toast.svelte';
  import Button from './Button.svelte';
  import Toaster from './Toaster.svelte';

  const { Story } = defineMeta({
    title: 'Components/Toast',
    component: Toaster,
    tags: ['autodocs'],
    parameters: { layout: 'fullscreen' },
  });

  function showBoth() {
    toasts.length = 0;
    toast('Sarcasm set to 80 in Book club', 'done', 0);
    toast("Not saved: Dom didn't answer. Check he's running, then try again.", 'error', 0);
  }
</script>

<Story name="Done and error" asChild play={showBoth}>
  <div class="pad">
    <Button onclick={() => toast('Preset changed')}>Show a toast</Button>
    <Button variant="danger" onclick={() => toast("Couldn't reach Dom.", 'error')}>Show an error</Button>
  </div>
  <Toaster />
</Story>

<style>
  .pad {
    display: flex;
    gap: 8px;
    padding: 16px;
    min-height: 320px;
  }
</style>
