<script lang="ts">
  import Info from "@lucide/svelte/icons/info";
  import { mergeProps } from "bits-ui";
  import type { Snippet } from "svelte";
  import * as Tooltip from "$lib/components/ui/tooltip";
  import { cn } from "$lib/utils";

  // The glyph rule (E80): descriptive text that would otherwise sit on screen all the time folds into this glyph.
  // Hover or keyboard focus shows it; a tap or a click pins it open, so phones can read it too. Critical
  // warnings are never folded: they stay as text (see web/README.md).
  let { label, children, class: className }: { label: string; children: Snippet; class?: string } = $props();
  let open = $state(false);
  // A click or tap pins the text open until the next click on the glyph, a click or tap anywhere else, Esc, or
  // focus leaving the glyph.
  let pinned = $state(false);
  let trigger = $state<HTMLElement | null>(null);
  function unpin() {
    pinned = false;
    open = false;
  }
  const own = {
    onclick: () => (pinned ? unpin() : (pinned = open = true)),
    onblur: unpin,
  };
</script>

<Tooltip.Root {open} onOpenChange={(value) => (open = value || pinned)} disableCloseOnTriggerClick>
  <Tooltip.Trigger bind:ref={trigger}>
    {#snippet child({ props })}
      <button
        {...mergeProps(props, own)}
        type="button"
        aria-label={label}
        aria-expanded={pinned}
        class={cn(
          "inline-grid size-6 shrink-0 place-items-center rounded-full align-middle text-muted-foreground transition-colors duration-(--motion-quick) hover:bg-raised hover:text-signal-strong data-[state=delayed-open]:text-signal-strong data-[state=instant-open]:text-signal-strong",
          className,
        )}
      >
        <Info class="size-3.5" aria-hidden="true" />
      </button>
    {/snippet}
  </Tooltip.Trigger>
  <!-- A tap elsewhere (iOS never blurs the glyph) or Esc closes it even while pinned. A tap on the glyph itself is
       its own click, which toggles. -->
  <Tooltip.Content
    class="max-w-72 text-left leading-relaxed"
    onInteractOutside={(event) => {
      if (!(event.target instanceof Node && trigger?.contains(event.target))) unpin();
    }}
    onEscapeKeydown={unpin}>{@render children()}</Tooltip.Content
  >
</Tooltip.Root>
