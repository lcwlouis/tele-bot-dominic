<script lang="ts">
  import { cn } from "$lib/utils";
  import { STATUS, type Status } from "./status";

  // A status as a glyph, named for screen readers. With `word`, the label shows beside it too.
  let { status, word = false, class: className }: { status: Status; word?: boolean; class?: string } = $props();
  const meta = $derived(STATUS[status]);
</script>

<span class={cn("inline-flex shrink-0 items-center gap-1.5", meta.tone, className)} data-status={status}>
  <meta.icon class={cn("size-4", status === "running" && "status-spin")} aria-hidden="true" />
  {#if word}<span class="text-meta">{meta.label}</span>{:else}<span class="sr-only">{meta.label}</span>{/if}
</span>
