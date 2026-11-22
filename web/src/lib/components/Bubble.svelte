<script lang="ts">
  import type { Snippet } from 'svelte';

  /*
    A bubble is Dom talking. Use it only for his own words (a reply, his reason for today's vibe);
    everything the dashboard says sits outside bubbles.
  */
  interface Props {
    size?: 'sm' | 'lg';
    /** Play "typing…" first, then show the words. The dashboard's one entrance; use it once per page. */
    typing?: boolean;
    children: Snippet;
  }

  let { size = 'sm', typing = false, children }: Props = $props();
</script>

<div class="bubble {size}" class:typing>
  {#if typing}
    <span class="dots" aria-hidden="true"><span></span><span></span><span></span></span>
  {/if}
  <p class="words">{@render children()}</p>
</div>

<style>
  .bubble {
    position: relative;
    display: inline-block;
    max-width: min(100%, 36em);
    padding: var(--space-2) var(--space-3);
    background: var(--bubble);
    color: var(--bubble-ink);
    border-radius: var(--radius-bubble);
    overflow-wrap: anywhere;
  }

  .lg {
    padding: var(--space-4) var(--space-5);
    font-family: var(--font-display);
    font-size: var(--step-1);
    font-weight: 500;
    line-height: 1.3;
    letter-spacing: -0.01em;
  }

  .words {
    margin: 0;
  }

  /* Typing dots hold the bubble's place for a beat, then the words pop in. Pure CSS: no timers. */
  .typing .words {
    animation: words-in var(--dur-med) var(--ease-pop) 760ms both;
  }

  .dots {
    position: absolute;
    left: var(--space-5);
    top: 50%;
    display: flex;
    gap: 5px;
    translate: 0 -50%;
    animation: dots-out 1ms linear 760ms both;
  }

  .dots span {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
    animation: bob 600ms var(--ease-out) infinite alternate;
  }

  .dots span:nth-child(2) {
    animation-delay: 150ms;
  }

  .dots span:nth-child(3) {
    animation-delay: 300ms;
  }

  @keyframes bob {
    from {
      opacity: 0.35;
      translate: 0 2px;
    }
    to {
      opacity: 1;
      translate: 0 -2px;
    }
  }

  @keyframes dots-out {
    to {
      visibility: hidden;
    }
  }

  @keyframes words-in {
    from {
      opacity: 0;
      scale: 0.96;
    }
    to {
      opacity: 1;
      scale: 1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .dots {
      display: none;
    }
  }
</style>
