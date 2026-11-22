<script lang="ts">
  import Button from './Button.svelte';
  import Icon from './Icon.svelte';

  /* Says what went wrong and what to do next. No apologies, nothing vague. */
  interface Props {
    title: string;
    /** What happened and what to do, e.g. "Dom didn't answer. Check he's running, then try again." */
    body: string;
    /** Shows a "Try again" button. */
    onretry?: () => void;
  }

  let { title, body, onretry }: Props = $props();
</script>

<div class="error" role="alert">
  <span class="icon"><Icon name="alert" /></span>
  <div class="text">
    <p class="title">{title}</p>
    <p class="body">{body}</p>
    {#if onretry}
      <div class="action"><Button size="sm" onclick={onretry}>Try again</Button></div>
    {/if}
  </div>
</div>

<style>
  .error {
    display: flex;
    gap: var(--space-3);
    padding: var(--space-4);
    border-radius: var(--radius-md);
    background: var(--cherry-soft);
  }

  .icon {
    color: var(--cherry);
  }

  .title {
    font-weight: 700;
    color: var(--cherry);
  }

  .body {
    margin-top: var(--space-1);
    max-width: 52ch;
  }

  .action {
    margin-top: var(--space-3);
  }
</style>
