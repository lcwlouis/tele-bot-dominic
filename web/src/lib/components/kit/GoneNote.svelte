<script lang="ts">
  import SearchX from "@lucide/svelte/icons/search-x";
  import X from "@lucide/svelte/icons/x";
  import IconButton from "./IconButton.svelte";

  // A link (a Telegram notice, a search hit) named an item this page no longer has: say so quietly, once, and let
  // the owner dismiss it. Not an error: the item was deleted or has aged out.
  let { text = "That item is gone. It may have been deleted.", ondismiss }: { text?: string; ondismiss: () => void } = $props();
</script>

<p role="status" class="mb-4 flex items-center gap-2 rounded-lg border border-dashed bg-surface py-1.5 pr-1.5 pl-3 text-meta text-muted-foreground">
  <SearchX class="size-4 shrink-0" aria-hidden="true" />
  <span class="min-w-0 flex-1">{text}</span>
  <IconButton label="Dismiss" icon={X} onclick={ondismiss} />
</p>
