<script lang="ts">
  import Badge from './Badge.svelte';

  interface Props {
    label: string;
    /** 0 to 1. */
    value?: number;
    /** Set for this chat rather than inherited. */
    custom?: boolean;
    disabled?: boolean;
    /** Fires when the owner lets go, not on every step. */
    onchange?: (value: number) => void;
  }

  let { label, value = $bindable(0.5), custom = false, disabled = false, onchange }: Props = $props();
  const id = $props.id();

  const percent = $derived(Math.round(value * 100));
</script>

<div class="slider">
  <div class="head">
    <label for={id}>{label}</label>
    {#if custom}<Badge tone="dom">Set here</Badge>{/if}
    <output for={id} class="num">{percent}</output>
  </div>
  <input
    {id}
    type="range"
    min="0"
    max="1"
    step="0.05"
    bind:value
    {disabled}
    aria-valuetext="{percent} out of 100"
    style:--fill="{percent}%"
    onchange={() => onchange?.(value)}
  />
</div>

<style>
  .slider {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .head {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  label {
    font-weight: 700;
  }

  output {
    margin-left: auto;
    color: var(--ink-muted);
    font-weight: 700;
  }

  input {
    --fill: 50%;
    width: 100%;
    height: var(--hit);
    margin: 0;
    background: transparent;
    appearance: none;
    cursor: pointer;
  }

  input:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  input::-webkit-slider-runnable-track {
    height: 8px;
    border-radius: var(--radius-pill);
    background: linear-gradient(to right, var(--grape) var(--fill), var(--surface-sunk) var(--fill));
  }

  input::-moz-range-track {
    height: 8px;
    border-radius: var(--radius-pill);
    background: linear-gradient(to right, var(--grape) var(--fill), var(--surface-sunk) var(--fill));
  }

  input::-webkit-slider-thumb {
    appearance: none;
    width: 26px;
    height: 26px;
    margin-top: -9px;
    border: 3px solid var(--grape);
    border-radius: 50%;
    background: var(--surface);
    transition: scale var(--dur-fast) var(--ease-out);
  }

  input::-moz-range-thumb {
    width: 20px;
    height: 20px;
    border: 3px solid var(--grape);
    border-radius: 50%;
    background: var(--surface);
  }

  input:active::-webkit-slider-thumb {
    scale: 1.15;
  }

  input:focus-visible {
    outline: none;
  }

  input:focus-visible::-webkit-slider-thumb {
    outline: 2px solid var(--grape);
    outline-offset: 3px;
  }

  input:focus-visible::-moz-range-thumb {
    outline: 2px solid var(--grape);
    outline-offset: 3px;
  }
</style>
