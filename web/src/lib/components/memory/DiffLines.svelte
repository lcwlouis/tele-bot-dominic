<script lang="ts">
  import { cn } from "$lib/utils";

  // A proposed change as lines: "-" removed, "+" added, anything else context. Colour never carries it alone: the
  // sign is in the text too. A unified diff's file and hunk headers are left out, unless `all`: a code review must
  // never hide a changed line that merely looks like a header.
  let { lines, all = false }: { lines: string[]; all?: boolean } = $props();
  const shown = $derived(all ? lines : lines.filter((line) => !/^(---|\+\+\+|@@)/.test(line)));
</script>

<div role="group" aria-label="Proposed changes" class="grid gap-1 font-mono text-[0.8rem] leading-relaxed">
  {#each shown as line, index (index)}
    {@const added = line.startsWith("+")}
    {@const removed = line.startsWith("-")}
    <p
      class={cn(
        "px-3 break-words whitespace-pre-wrap text-muted-foreground",
        (added || removed) && "rounded-lg border py-1.5",
        added && "border-signal/45 bg-tint text-signal-strong",
        removed && "border-coral/45 bg-coral-tint text-coral",
      )}
    >
      {line || " "}
    </p>
  {/each}
</div>
