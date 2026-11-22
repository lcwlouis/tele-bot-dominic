<script lang="ts">
  import type { Snippet } from 'svelte';

  /* An empty list is an invitation: say what will show up here and how it gets here. */
  interface Props {
    title: string;
    body?: string;
    action?: Snippet;
  }

  let { title, body = '', action }: Props = $props();
</script>

<div class="empty">
  <p class="title">{title}</p>
  {#if body}<p class="body">{body}</p>{/if}
  {#if action}<div class="action">{@render action()}</div>{/if}
</div>

<style>
  .empty {
    padding: var(--space-5) var(--space-4);
    border: 1.5px dashed var(--line-strong);
    border-radius: var(--radius-md);
  }

  .title {
    font-weight: 700;
  }

  .body {
    margin-top: var(--space-1);
    color: var(--ink-muted);
    max-width: 52ch;
  }

  .action {
    margin-top: var(--space-4);
  }
</style>
