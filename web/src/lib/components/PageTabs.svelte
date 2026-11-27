<script lang="ts" module>
  // Where each destination's strip was scrolled to, so the next tab's page starts it there: the tab tapped was in
  // view, so it stays put instead of snapping back to the left and sliding over.
  const kept = new Map<string, number>();
</script>

<script lang="ts">
  import type { Destination, Tab } from "$lib/nav";
  import { cn } from "$lib/utils";

  // A destination's pages (System: Settings, Plugins, Usage, Turn log), as links under the page header.
  let { destination, tab }: { destination: Destination; tab?: Tab } = $props();

  // Each page mounts its own strip. Before it paints, start it where the last one was and bring the current tab
  // into view if it isn't: set directly, so it never animates or shifts after the page appears.
  let pill = $state<HTMLUListElement | null>(null);
  $effect(() => {
    void tab?.id;
    const strip = pill;
    const current = strip?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!strip || !current) return;
    const reveal = (from: number) => {
      let scroll = from;
      const left = current.offsetLeft; // the strip is the tab's offset parent
      const right = left + current.offsetWidth;
      if (left < scroll) scroll = left - 4;
      else if (right > scroll + strip.clientWidth) scroll = right - strip.clientWidth + 4;
      if (scroll !== strip.scrollLeft) strip.scrollLeft = scroll;
      kept.set(destination.id, strip.scrollLeft);
    };
    reveal(kept.get(destination.id) ?? 0);
    // The strip can narrow or its tabs widen after this first placement (a classic scrollbar appearing once the page
    // grows taller, a late font): bring the tab back into view then too, from where the strip is.
    const resized = new ResizeObserver(() => reveal(strip.scrollLeft));
    resized.observe(strip);
    for (const link of strip.querySelectorAll("a")) resized.observe(link);
    return () => resized.disconnect();
  });

  function remember() {
    if (pill) kept.set(destination.id, pill.scrollLeft);
  }
</script>

{#if destination.tabs}
  <nav aria-label={`${destination.label} pages`} class="mb-6">
    <!-- The pill itself scrolls on a narrow screen, so no scrollbar shows outside it. -->
    <ul bind:this={pill} onscroll={remember} class="no-scrollbar relative [scroll-behavior:auto] flex w-fit max-w-full gap-1 overflow-x-auto rounded-xl border bg-surface p-1 [&>li]:shrink-0">
      {#each destination.tabs as item (item.id)}
        {@const here = item.id === tab?.id}
        <li>
          <a
            href={item.href}
            aria-current={here ? "page" : undefined}
            class={cn(
              "flex h-9 items-center gap-2 rounded-[0.6rem] px-3 text-sm font-medium text-muted-foreground transition-colors hover:bg-raised hover:text-foreground",
              here && "bg-tint text-signal-strong hover:bg-tint hover:text-signal-strong",
            )}
          >
            <item.icon class="size-4" aria-hidden="true" />
            {item.label}
          </a>
        </li>
      {/each}
    </ul>
  </nav>
{/if}
