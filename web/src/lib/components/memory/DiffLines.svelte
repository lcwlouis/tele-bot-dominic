<script lang="ts">
  import { highlight, languageOf } from "$lib/highlight";
  import { cn } from "$lib/utils";

  // A proposed change as lines: "-" removed, "+" added, anything else context. Colour never carries it alone: the
  // sign is in the text too. A unified diff's file and hunk headers are left out, unless `all`: a code review must
  // never hide a changed line that merely looks like a header. With a `language` or a `path`, each line's code is
  // highlighted, while the line keeps its added or removed tint, border and sign.
  let {
    lines,
    all = false,
    language = null,
    path = null,
  }: { lines: string[]; all?: boolean; language?: string | null; path?: string | null } = $props();
  // Headers sit in known places: "--- " and "+++ " as the first two lines, hunks as "@@ -a,b +c,d @@". A removed
  // Markdown rule ("----") or any other changed line that starts the same way still shows.
  const header = (line: string, index: number) =>
    (index < 2 && /^(--- |\+\+\+ )/.test(line)) || /^@@ -\d+(,\d+)? \+\d+(,\d+)? @@/.test(line);
  const shown = $derived(all ? lines : lines.filter((line, index) => !header(line, index)));
  const lang = $derived(languageOf(language) ?? languageOf(path));
  const signed = (line: string) => /^[-+ ]/.test(line);
</script>

<div role="group" aria-label="Proposed changes" class="code grid gap-1 font-mono text-[0.8rem] leading-relaxed">
  {#each shown as line, index (index)}
    {@const added = line.startsWith("+")}
    {@const removed = line.startsWith("-")}
    {@const html = lang && signed(line) ? highlight(line.slice(1), lang) : null}
    <p
      class={cn(
        "px-3 break-words whitespace-pre-wrap text-muted-foreground",
        (added || removed) && "rounded-lg border py-1.5",
        added && "border-signal/45 bg-tint text-signal-strong",
        removed && "border-coral/45 bg-coral-tint text-coral",
      )}
    >
      {#if html !== null}<span class={cn(added || removed ? "font-semibold" : "")}>{line[0]}</span><span class="text-foreground">{@html html}</span>{:else}{line || " "}{/if}
    </p>
  {/each}
</div>
