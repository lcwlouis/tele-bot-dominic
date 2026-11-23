<script lang="ts">
  import { getJSON, type SearchData } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { search } from "$lib/nav";
  import { route } from "$lib/router.svelte";

  const query = $derived((new URLSearchParams(route.search).get("q") ?? "").trim());
  let data = $state<SearchData | null>(null);
  let failed = $state("");
  let loading = $state(false);

  async function load(text: string, signal: AbortSignal) {
    loading = true;
    failed = "";
    try {
      data = await getJSON<SearchData>(`/api/search?q=${encodeURIComponent(text)}`, signal);
    } catch (error) {
      if (signal.aborted) return;
      failed = error instanceof Error ? error.message : "Unknown error";
    } finally {
      if (!signal.aborted) loading = false;
    }
  }

  $effect(() => {
    const text = query;
    if (!text) {
      data = null;
      failed = "";
      loading = false;
      return;
    }
    const controller = new AbortController();
    load(text, controller.signal);
    return () => controller.abort();
  });
</script>

<PageHeader kicker="Everything" title={search.label} subtitle={search.blurb} />

<p class="mb-5 text-sm text-muted-foreground" aria-live="polite">
  {#if !query}Type in the search box to look through Dom.{:else if loading}Searching for “{query}”…{:else if data}Results for “{data.query}”.{/if}
</p>

{#if failed}
  <StatusNote tone="error" title="Couldn't search" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="outline" onclick={() => load(query, new AbortController().signal)}>Try again</Button>
  </StatusNote>
{:else if query && data && !loading && data.groups.length === 0}
  <StatusNote title="Nothing found" detail="Try fewer or different words. Every word you type has to appear in the same item." />
{:else if data && query}
  <div class="grid gap-5">
    {#each data.groups as group (group.kind)}
      <section aria-labelledby={`search-${group.kind}`}>
        <Card.Root>
          <Card.Header>
            <Card.Title id={`search-${group.kind}`} class="text-lg">{group.label}</Card.Title>
          </Card.Header>
          <Card.Content>
            <ul class="grid gap-1">
              {#each group.hits as hit}
                <li>
                  <a href={hit.href} class="block rounded-[0.6rem] px-2.5 py-2 transition-colors outline-none hover:bg-raised focus-visible:ring-3 focus-visible:ring-ring/50">
                    <span class="flex flex-wrap items-center gap-2">
                      <span class="min-w-0 break-words font-medium">{hit.title}</span>
                      {#if !hit.full}<Badge variant="secondary">Preview</Badge>{/if}
                    </span>
                    <span class="mt-0.5 block text-sm break-words text-muted-foreground">{hit.text}</span>
                  </a>
                </li>
              {/each}
            </ul>
          </Card.Content>
        </Card.Root>
      </section>
    {/each}
  </div>
{:else if loading}
  <div class="h-40 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{/if}
