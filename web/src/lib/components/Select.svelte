<script lang="ts">
  import Icon from './Icon.svelte';

  interface Option {
    value: string;
    label: string;
  }

  interface Props {
    label: string;
    options: Option[];
    value?: string;
    /** One quiet line under the control. */
    hint?: string;
    /** Keep the label for screen readers only, when the context already says what this is. */
    hideLabel?: boolean;
    disabled?: boolean;
    onchange?: (value: string) => void;
  }

  let {
    label,
    options,
    value = $bindable(''),
    hint = '',
    hideLabel = false,
    disabled = false,
    onchange,
  }: Props = $props();
  const id = $props.id();
</script>

<div class="select">
  <label for={id} class:sr-only={hideLabel}>{label}</label>
  <div class="control">
    <select
      {id}
      bind:value
      {disabled}
      aria-describedby={hint ? `${id}-hint` : undefined}
      onchange={() => onchange?.(value)}
    >
      {#each options as option (option.value)}
        <option value={option.value}>{option.label}</option>
      {/each}
    </select>
    <span class="chevron" aria-hidden="true"><Icon name="chevron" size={20} /></span>
  </div>
  {#if hint}<p id="{id}-hint">{hint}</p>{/if}
</div>

<style>
  .select {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }

  label {
    font-weight: 700;
  }

  .control {
    position: relative;
  }

  select {
    width: 100%;
    min-height: var(--hit);
    padding: 0 calc(var(--space-4) + 24px) 0 var(--space-3);
    border: 1.5px solid var(--line-strong);
    border-radius: var(--radius-sm);
    background: var(--surface);
    appearance: none;
    cursor: pointer;
    text-overflow: ellipsis;
  }

  select:hover:not(:disabled) {
    border-color: var(--grape);
  }

  select:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .chevron {
    position: absolute;
    right: var(--space-3);
    top: 50%;
    translate: 0 -50%;
    color: var(--ink-muted);
    pointer-events: none;
  }

  p {
    color: var(--ink-muted);
    font-size: var(--step--1);
  }
</style>
