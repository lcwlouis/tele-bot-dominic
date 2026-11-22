<script lang="ts">
  interface Props {
    label: string;
    value: string;
    /** One quiet line under the value. */
    hint?: string;
    /** 0 to 1: draws a fill bar, e.g. spend against the cap. Tone follows how full it is. */
    fill?: number | null;
  }

  let { label, value, hint = '', fill = null }: Props = $props();

  const ratio = $derived(fill === null ? null : Math.min(Math.max(fill, 0), 1));
  const tone = $derived(fill === null ? '' : fill >= 1 ? 'danger' : fill >= 0.7 ? 'waiting' : 'live');
</script>

<div class="stat">
  <span class="label">{label}</span>
  <span class="value num">{value}</span>
  {#if ratio !== null}
    <svg class="bar {tone}" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true">
      <rect class="track" width="100" height="6" rx="3" />
      <rect class="fill" width={Math.max(ratio * 100, 3)} height="6" rx="3" />
    </svg>
  {/if}
  {#if hint}<span class="hint">{hint}</span>{/if}
</div>

<style>
  .stat {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }

  .label {
    color: var(--ink-muted);
    font-size: var(--step--1);
    font-weight: 700;
  }

  .value {
    font-family: var(--font-display);
    font-size: var(--step-2);
    font-weight: 600;
    line-height: var(--leading-tight);
    overflow-wrap: anywhere;
  }

  .hint {
    color: var(--ink-muted);
    font-size: var(--step--1);
  }

  .bar {
    width: 100%;
    height: 6px;
    margin-top: var(--space-1);
  }

  .track {
    fill: var(--surface-sunk);
  }

  .live .fill {
    fill: var(--mint-fill);
  }

  .waiting .fill {
    fill: var(--mango-fill);
  }

  .danger .fill {
    fill: var(--cherry);
  }
</style>
