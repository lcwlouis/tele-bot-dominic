<script lang="ts">
  import { dismiss, toasts } from '../toast.svelte';
  import Icon from './Icon.svelte';
</script>

<!-- Always in the page, so screen readers hear each toast as it is added. -->
<div class="toaster" role="status" aria-live="polite">
  {#each toasts as item (item.id)}
    <div class="toast {item.tone}">
      <span class="mark"><Icon name={item.tone === 'error' ? 'alert' : 'check'} size={20} /></span>
      <p>{item.text}</p>
      <button type="button" onclick={() => dismiss(item.id)}>
        <Icon name="close" size={18} label="Dismiss" />
      </button>
    </div>
  {/each}
</div>

<style>
  .toaster {
    position: fixed;
    z-index: 20;
    left: var(--space-4);
    right: var(--space-4);
    bottom: calc(var(--tabbar) + var(--space-3) + env(safe-area-inset-bottom));
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    pointer-events: none;
  }

  @media (min-width: 960px) {
    .toaster {
      left: auto;
      right: var(--space-5);
      bottom: var(--space-5);
      width: 380px;
    }
  }

  .toast {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-2) var(--space-2) var(--space-4);
    border-radius: var(--radius-md);
    background: var(--ink);
    color: var(--paper);
    box-shadow: var(--shadow-pop);
    pointer-events: auto;
    animation: pop var(--dur-med) var(--ease-pop);
  }

  /* The toast is inverted (ink on paper's place), so the soft tints read as the strong colour here. */
  .done .mark {
    color: var(--mint-soft);
  }

  .error .mark {
    color: var(--cherry-soft);
  }

  p {
    flex: 1;
    font-weight: 700;
  }

  button {
    display: grid;
    place-items: center;
    width: var(--hit);
    height: var(--hit);
    border: 0;
    border-radius: var(--radius-pill);
    background: transparent;
    color: inherit;
    cursor: pointer;
  }

  @keyframes pop {
    from {
      opacity: 0;
      translate: 0 8px;
      scale: 0.97;
    }
  }
</style>
