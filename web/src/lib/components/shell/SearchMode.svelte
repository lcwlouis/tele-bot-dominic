<script lang="ts">
  import * as Tooltip from "$lib/components/ui/tooltip";
  import { cn } from "$lib/utils";

  // The search mode, a small two-way switch that sits inside the search field's right edge (D19). Each side shows
  // its word, carries an aria-label and has a tooltip saying what it searches.
  let { meaning, onchange, class: className }: { meaning: boolean; onchange: (meaning: boolean) => void; class?: string } = $props();
  const modes = [
    { meaning: false, word: "Words", label: "Search by words", tip: "Finds items that contain every word you type" },
    { meaning: true, word: "Meaning", label: "Search by meaning", tip: "Finds items close in meaning, even with other words" },
  ];
</script>

<div role="group" aria-label="Search mode" class={cn("flex items-center rounded-[0.45rem] bg-raised p-0.5", className)}>
  {#each modes as mode (mode.word)}
    {@const on = mode.meaning === meaning}
    <Tooltip.Root>
      <Tooltip.Trigger
        type="button"
        aria-pressed={on}
        aria-label={mode.label}
        onclick={() => !on && onchange(mode.meaning)}
        class={cn(
          "h-6 rounded-[0.35rem] px-2 text-caption font-medium transition-colors duration-(--motion-quick) outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
          on ? "bg-surface text-foreground shadow-xs" : "text-muted-foreground hover:text-foreground",
        )}
      >
        {mode.word}
      </Tooltip.Trigger>
      <Tooltip.Content>{mode.tip}</Tooltip.Content>
    </Tooltip.Root>
  {/each}
</div>
