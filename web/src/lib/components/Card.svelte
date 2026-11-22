<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    title: string;
    /** Short fact beside the title, e.g. a count. */
    meta?: string;
    /** waiting: something in here needs you (mango edge). */
    tone?: 'plain' | 'waiting';
    /** A button or link beside the title. */
    action?: Snippet;
    /** Content goes flush to the edges, for lists of rows. */
    flush?: boolean;
    children: Snippet;
  }

  let { title, meta = '', tone = 'plain', action, flush = false, children }: Props = $props();
  const id = $props.id();
</script>

<section class="card {tone}" aria-labelledby="{id}-title">
  <header>
    <h2 id="{id}-title">{title}</h2>
    {#if meta}<span class="meta">{meta}</span>{/if}
    {#if action}<div class="action">{@render action()}</div>{/if}
  </header>
  <div class="body" class:flush>
    {@render children()}
  </div>
</section>

<style>
  .card {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    min-width: 0;
  }

  .waiting {
    border-left: 4px solid var(--mango-fill);
  }

  header {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-3);
    padding: var(--space-4) var(--space-4) var(--space-3);
  }

  .meta {
    color: var(--ink-muted);
    font-size: var(--step--1);
    font-weight: 700;
  }

  .action {
    margin-left: auto;
  }

  .body {
    padding: 0 var(--space-4) var(--space-4);
  }

  .body.flush {
    padding: 0;
  }
</style>
