<script lang="ts">
  interface Props {
    label: string;
    /** What turning it on does, in one sentence. */
    description?: string;
    checked?: boolean;
    disabled?: boolean;
    onchange?: (checked: boolean) => void;
  }

  let { label, description = '', checked = $bindable(false), disabled = false, onchange }: Props = $props();
  const id = $props.id();

  function flip() {
    checked = !checked;
    onchange?.(checked);
  }
</script>

<div class="toggle">
  <div class="text">
    <label id="{id}-label" for={id}>{label}</label>
    {#if description}<p id="{id}-desc">{description}</p>{/if}
  </div>
  <button
    {id}
    type="button"
    role="switch"
    aria-checked={checked}
    aria-labelledby="{id}-label"
    aria-describedby={description ? `${id}-desc` : undefined}
    {disabled}
    onclick={flip}
  >
    <span class="knob" aria-hidden="true"></span>
  </button>
</div>

<style>
  .toggle {
    display: flex;
    align-items: flex-start;
    gap: var(--space-4);
  }

  .text {
    flex: 1;
    min-width: 0;
  }

  label {
    font-weight: 700;
    cursor: pointer;
  }

  p {
    color: var(--ink-muted);
    font-size: var(--step--1);
    max-width: 52ch;
  }

  button {
    position: relative;
    flex: none;
    width: 52px;
    height: 32px;
    margin-top: 2px;
    padding: 0;
    border: 1.5px solid var(--line-strong);
    border-radius: var(--radius-pill);
    background: var(--surface-sunk);
    cursor: pointer;
    transition:
      background-color var(--dur-fast) var(--ease-out),
      border-color var(--dur-fast) var(--ease-out);
  }

  /* Full-size touch target around the 32 px track. */
  button::after {
    content: '';
    position: absolute;
    inset: -7px -4px;
  }

  .knob {
    position: absolute;
    top: 3px;
    left: 3px;
    width: 23px;
    height: 23px;
    border-radius: 50%;
    background: var(--ink-muted);
    transition: translate var(--dur-med) var(--ease-pop);
  }

  button[aria-checked='true'] {
    background: var(--grape);
    border-color: var(--grape);
  }

  button[aria-checked='true'] .knob {
    translate: 20px 0;
    background: var(--grape-ink);
  }

  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
