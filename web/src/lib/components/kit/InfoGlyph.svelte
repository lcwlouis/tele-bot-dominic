<script lang="ts">
  import Info from "@lucide/svelte/icons/info";
  import type { Snippet } from "svelte";
  import * as Tooltip from "$lib/components/ui/tooltip";
  import { cn } from "$lib/utils";

  // The glyph rule (E80): descriptive text that would otherwise sit on screen all the time folds into this glyph.
  // Hover or keyboard focus shows it; a tap or a click pins it open, so phones can read it too. Critical
  // warnings are never folded: they stay as text (see web/README.md).
  let { label, children, class: className }: { label: string; children: Snippet; class?: string } = $props();
  let open = $state(false);
  // A click or tap pins the text open until the next click, Esc or focus leaving the glyph.
  let pinned = $state(false);
  function unpin() {
    pinned = false;
    open = false;
  }
</script>

<Tooltip.Root {open} onOpenChange={(value) => (open = value || pinned)} disableCloseOnTriggerClick>
  <Tooltip.Trigger>
    {#snippet child({ props })}
      <button
        {...props}
        type="button"
        aria-label={label}
        aria-expanded={pinned}
        class={cn(
          "inline-grid size-6 shrink-0 place-items-center rounded-full align-middle text-muted-foreground transition-colors duration-(--motion-quick) hover:bg-raised hover:text-signal-strong data-[state=delayed-open]:text-signal-strong data-[state=instant-open]:text-signal-strong",
          className,
        )}
        onclick={() => (pinned ? unpin() : (pinned = open = true))}
        onblur={unpin}
        onkeydown={(event) => event.key === "Escape" && unpin()}
      >
        <Info class="size-3.5" aria-hidden="true" />
      </button>
    {/snippet}
  </Tooltip.Trigger>
  <Tooltip.Content class="max-w-72 text-left leading-relaxed">{@render children()}</Tooltip.Content>
</Tooltip.Root>
