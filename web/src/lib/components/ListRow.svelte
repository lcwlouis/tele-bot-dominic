<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    title: string;
    /** One quiet line under the title. */
    meta?: string;
    /** Left of the title: a presence mark, an icon. */
    lead?: Snippet;
    /** Right edge: a count, a badge, a button. */
    trail?: Snippet;
    /** Below the title line, full width. */
    children?: Snippet;
  }

  let { title, meta = '', lead, trail, children }: Props = $props();
</script>

<li class="row">
  <div class="line">
    {#if lead}<div class="lead">{@render lead()}</div>{/if}
    <div class="text">
      <span class="title">{title}</span>
      {#if meta}<span class="meta">{meta}</span>{/if}
    </div>
    {#if trail}<div class="trail">{@render trail()}</div>{/if}
  </div>
  {#if children}<div class="more">{@render children()}</div>{/if}
</li>

<style>
  .row {
    padding: var(--space-3) var(--space-4);
    border-top: 1px solid var(--line);
  }

  .line {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    min-height: 32px;
  }

  .lead {
    flex: none;
    display: flex;
  }

  .text {
    display: flex;
    flex-direction: column;
    min-width: 0;
    flex: 1;
  }

  .title {
    font-weight: 700;
    overflow-wrap: anywhere;
  }

  .meta {
    color: var(--ink-muted);
    font-size: var(--step--1);
  }

  .trail {
    flex: none;
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .more {
    margin-top: var(--space-2);
  }
</style>
