<script lang="ts">
  import { tokens } from "$lib/markdown";
  import { cn } from "$lib/utils";
  import Blocks from "./Blocks.svelte";

  // A markdown document as safe elements: see $lib/markdown for what is kept and what shows as text.
  let { text, class: className = "" }: { text: string; class?: string } = $props();
  const blocks = $derived(tokens(text));
</script>

<div class={cn("markdown", className)}><Blocks tokens={blocks} /></div>
