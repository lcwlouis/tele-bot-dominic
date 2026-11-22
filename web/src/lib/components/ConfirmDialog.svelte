<script lang="ts">
  import type { Snippet } from 'svelte';
  import Button from './Button.svelte';

  /*
    Asks before anything hard to undo. Built on the native <dialog>, so focus is trapped, Escape
    cancels, and the rest of the page is inert while it is open.
  */
  interface Props {
    open?: boolean;
    title: string;
    /** Says the action, e.g. "Leave group". The toast afterwards uses the same verb. */
    confirmLabel: string;
    cancelLabel?: string;
    tone?: 'primary' | 'danger';
    onconfirm: () => void;
    children: Snippet;
  }

  let {
    open = $bindable(false),
    title,
    confirmLabel,
    cancelLabel = 'Cancel',
    tone = 'danger',
    onconfirm,
    children,
  }: Props = $props();

  let dialog: HTMLDialogElement;
  const id = $props.id();

  $effect(() => {
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  });

  function confirm() {
    open = false;
    onconfirm();
  }

  /* A press on the backdrop (the dialog box itself, outside its panel) cancels. */
  function backdrop(event: MouseEvent) {
    if (event.target === dialog) open = false;
  }
</script>

<dialog
  bind:this={dialog}
  aria-labelledby="{id}-title"
  aria-describedby="{id}-body"
  onclose={() => (open = false)}
  onclick={backdrop}
>
  <div class="panel">
    <h2 id="{id}-title">{title}</h2>
    <div id="{id}-body" class="body">{@render children()}</div>
    <div class="actions">
      <Button variant="secondary" onclick={() => (open = false)}>{cancelLabel}</Button>
      <Button variant={tone} onclick={confirm}>{confirmLabel}</Button>
    </div>
  </div>
</dialog>

<style>
  dialog {
    width: min(100% - 2 * var(--space-4), 420px);
    padding: 0;
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    background: var(--surface);
    color: var(--ink);
    box-shadow: var(--shadow-pop);
  }

  dialog::backdrop {
    background: var(--scrim);
  }

  dialog[open] {
    animation: rise var(--dur-med) var(--ease-pop);
  }

  .panel {
    padding: var(--space-5);
  }

  .body {
    margin-top: var(--space-2);
    color: var(--ink-muted);
  }

  .actions {
    display: flex;
    flex-wrap: wrap-reverse;
    justify-content: flex-end;
    gap: var(--space-2);
    margin-top: var(--space-5);
  }

  @keyframes rise {
    from {
      opacity: 0;
      translate: 0 12px;
    }
  }
</style>
