<script lang="ts">
  import ArrowUpRight from "@lucide/svelte/icons/arrow-up-right";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import type { Destination, Tab } from "$lib/nav";
  import { cn } from "$lib/utils";

  // Destinations not ported yet: the baseline shows where each page lives and links to the current page.
  let { destination, tab }: { destination: Destination; tab?: Tab } = $props();
  const page = $derived(tab ?? destination);
  const legacy = $derived(tab?.legacy ?? destination.legacy ?? "/");
</script>

<PageHeader kicker={tab ? destination.label : "Destination"} title={page.label} subtitle={page.blurb} />

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

<Card.Root class="orbit-grid max-w-2xl">
  <Card.Header>
    <p class="kicker">Not ported yet</p>
    <Card.Title class="text-lg">{page.label} still lives in the current dashboard</Card.Title>
    <Card.Description>
      {page.label} moves to the new dashboard in a later update. Until then the current page has everything.
    </Card.Description>
  </Card.Header>
  <Card.Footer>
    <Button href={legacy} variant="secondary">
      Open {page.label} in the current dashboard <ArrowUpRight data-icon="inline-end" aria-hidden="true" />
    </Button>
  </Card.Footer>
</Card.Root>
