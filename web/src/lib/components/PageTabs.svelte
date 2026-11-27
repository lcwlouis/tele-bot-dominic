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
    const current = pill?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!pill || !current) return;
    let scroll = kept.get(destination.id) ?? 0;
    const left = current.offsetLeft; // the strip is the tab's offset parent
    const right = left + current.offsetWidth;
    if (left < scroll) scroll = left - 4;
    else if (right > scroll + pill.clientWidth) scroll = right - pill.clientWidth + 4;
    pill.scrollLeft = scroll;
    kept.set(destination.id, pill.scrollLeft);
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
