<script lang="ts">
  import type { Destination, Tab } from "$lib/nav";
  import { cn } from "$lib/utils";

  // A destination's pages (System: Settings, Plugins, Usage, Turn log), as links under the page header.
  let { destination, tab }: { destination: Destination; tab?: Tab } = $props();
</script>

{#if destination.tabs}
  <nav aria-label={`${destination.label} pages`} class="mb-6">
    <!-- The pill itself scrolls on a narrow screen, so no scrollbar shows outside it. -->
    <ul class="no-scrollbar flex w-fit max-w-full gap-1 overflow-x-auto rounded-xl border bg-surface p-1 [&>li]:shrink-0">
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
