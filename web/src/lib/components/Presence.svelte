<script lang="ts">
  import type { Presence } from '../api/types';

  interface Props {
    presence: Presence;
    /** Show the word next to the dot. Without it the word is still read out. */
    showLabel?: boolean;
  }

  let { presence, showLabel = true }: Props = $props();

  const LABEL: Record<Presence, string> = { online: 'Online', offline: 'Offline', asleep: 'Asleep' };
</script>

<span class="presence {presence}">
  <span class="mark" aria-hidden="true"></span>
  <span class={showLabel ? 'label' : 'sr-only'}>{LABEL[presence]}</span>
</span>

<style>
  .presence {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--step--1);
    font-weight: 700;
    color: var(--ink-muted);
  }

  .mark {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex: none;
  }

  /* Online: a solid mint dot with a halo. Offline: an empty ring. Asleep: a crescent. */
  .online .mark {
    background: var(--mint-fill);
    box-shadow: 0 0 0 3px var(--mint-soft);
  }

  .online .label {
    color: var(--mint);
  }

  .offline .mark {
    border: 2px solid var(--line-strong);
  }

  .asleep .mark {
    background: transparent;
    box-shadow: inset -3px -1px 0 0 var(--ink-muted);
  }
</style>
