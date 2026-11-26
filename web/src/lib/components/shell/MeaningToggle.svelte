<script lang="ts">
  import Sparkles from "@lucide/svelte/icons/sparkles";
  import * as Tooltip from "$lib/components/ui/tooltip";
  import { buttonVariants } from "$lib/components/ui/button";
  import { cn } from "$lib/utils";

  // Search by meaning instead of keywords (D19). A toggle with a visible word, so it needs no icon-only label; the
  // tooltip says what it changes.
  let { pressed, onclick }: { pressed: boolean; onclick: () => void } = $props();
</script>

<Tooltip.Root>
  <Tooltip.Trigger
    type="button"
    aria-pressed={pressed}
    {onclick}
    class={cn(buttonVariants({ variant: pressed ? "secondary" : "outline", size: "sm" }), "h-9 shrink-0", pressed && "text-signal-strong")}
  >
    <Sparkles aria-hidden="true" /> Meaning
  </Tooltip.Trigger>
  <Tooltip.Content>{pressed ? "Searching by meaning. Press for keywords." : "Search by meaning, not exact words"}</Tooltip.Content>
</Tooltip.Root>
