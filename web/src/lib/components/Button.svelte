<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLButtonAttributes } from 'svelte/elements';

  interface Props extends HTMLButtonAttributes {
    /** primary: the one main action. secondary: everything else. quiet: low-key. danger: destructive. */
    variant?: 'primary' | 'secondary' | 'quiet' | 'danger';
    size?: 'md' | 'sm';
    /** Shows a working state and blocks repeat presses. */
    busy?: boolean;
    /** Renders a link that looks like a button. */
    href?: string;
    children: Snippet;
  }

  let {
    variant = 'secondary',
    size = 'md',
    busy = false,
    href,
    type = 'button',
    disabled,
    children,
    ...rest
  }: Props = $props();
</script>

{#if href}
  <a class="button {variant} {size}" {href}>{@render children()}</a>
{:else}
  <button
    class="button {variant} {size}"
    {type}
    disabled={disabled || busy}
    aria-busy={busy || undefined}
    {...rest}
  >
    {#if busy}<span class="spinner" aria-hidden="true"></span>{/if}
    {@render children()}
  </button>
{/if}

<style>
  .button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    min-height: var(--hit);
    padding: 0 var(--space-5);
    border: 1.5px solid transparent;
    border-radius: var(--radius-pill);
    font-weight: 700;
    font-size: var(--step-0);
    line-height: 1;
    text-decoration: none;
    white-space: nowrap;
    cursor: pointer;
    transition:
      background-color var(--dur-fast) var(--ease-out),
      transform var(--dur-fast) var(--ease-out);
  }

  .button:active:not(:disabled) {
    transform: scale(0.97);
  }

  .button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .button[aria-busy='true'] {
    opacity: 1;
    cursor: progress;
  }

  .sm {
    position: relative;
    min-height: 36px;
    padding: 0 var(--space-4);
    font-size: var(--step--1);
  }

  /* A small button still gets a full-size touch target. */
  .sm::after {
    content: '';
    position: absolute;
    inset: -4px 0;
  }

  .primary {
    background: var(--grape);
    color: var(--grape-ink);
  }

  .primary:hover:not(:disabled) {
    background: var(--grape-strong);
  }

  .secondary {
    background: var(--surface);
    border-color: var(--line-strong);
    color: var(--ink);
  }

  .secondary:hover:not(:disabled) {
    border-color: var(--grape);
  }

  .quiet {
    background: transparent;
    color: var(--grape);
    padding-inline: var(--space-3);
  }

  .quiet:hover:not(:disabled) {
    background: var(--grape-soft);
  }

  .danger {
    background: transparent;
    border-color: var(--cherry);
    color: var(--cherry);
  }

  .danger:hover:not(:disabled) {
    background: var(--cherry-soft);
  }

  .spinner {
    width: 1em;
    height: 1em;
    border: 2px solid currentColor;
    border-right-color: transparent;
    border-radius: 50%;
    animation: spin 700ms linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
