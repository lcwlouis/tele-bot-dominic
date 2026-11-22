<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    /** neutral: plain fact. live: online, set, passing. waiting: needs you. danger: broken. dom: Dom's own pick. */
    tone?: 'neutral' | 'live' | 'waiting' | 'danger' | 'dom';
    children: Snippet;
  }

  let { tone = 'neutral', children }: Props = $props();
</script>

<span class="badge {tone}">{@render children()}</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 2px var(--space-2);
    border-radius: var(--radius-sm);
    font-size: var(--step--1);
    font-weight: 700;
    line-height: 1.4;
    white-space: nowrap;
  }

  .neutral {
    background: var(--surface-sunk);
    color: var(--ink-muted);
  }

  .live {
    background: var(--mint-soft);
    color: var(--mint);
  }

  .waiting {
    background: var(--mango-soft);
    color: var(--mango);
  }

  .danger {
    background: var(--cherry-soft);
    color: var(--cherry);
  }

  .dom {
    background: var(--grape-soft);
    color: var(--grape);
  }
</style>
