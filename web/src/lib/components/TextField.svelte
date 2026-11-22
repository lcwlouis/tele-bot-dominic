<script lang="ts">
  import type { HTMLInputAttributes } from 'svelte/elements';

  interface Props extends Omit<HTMLInputAttributes, 'value'> {
    label: string;
    value?: string;
    /** One quiet line under the field. */
    hint?: string;
    /** What is wrong and how to fix it. Replaces the hint while set. */
    error?: string;
  }

  let { label, value = $bindable(''), hint = '', error = '', type = 'text', ...rest }: Props = $props();
  const id = $props.id();
  const note = $derived(error || hint);
</script>

<div class="field" class:invalid={!!error}>
  <label for={id}>{label}</label>
  <input
    {id}
    {type}
    bind:value
    aria-invalid={error ? 'true' : undefined}
    aria-describedby={note ? `${id}-note` : undefined}
    spellcheck="false"
    autocapitalize="off"
    {...rest}
  />
  {#if note}<p id="{id}-note">{note}</p>{/if}
</div>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }

  label {
    font-weight: 700;
  }

  input {
    width: 100%;
    min-height: var(--hit);
    padding: 0 var(--space-3);
    border: 1.5px solid var(--line-strong);
    border-radius: var(--radius-sm);
    background: var(--surface);
  }

  input:hover {
    border-color: var(--grape);
  }

  p {
    color: var(--ink-muted);
    font-size: var(--step--1);
    overflow-wrap: anywhere;
  }

  .invalid input {
    border-color: var(--cherry);
  }

  .invalid p {
    color: var(--cherry);
    font-weight: 700;
  }
</style>
