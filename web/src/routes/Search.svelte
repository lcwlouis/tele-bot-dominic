<script lang="ts">
  import { getJSON, type SearchData } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { search, searchHref } from "$lib/nav";
  import MeaningToggle from "$lib/components/shell/MeaningToggle.svelte";
  import { navigate, route } from "$lib/router.svelte";

  const query = $derived((new URLSearchParams(route.search).get("q") ?? "").trim());
  const meaning = $derived(new URLSearchParams(route.search).get("mode") === "meaning");
  let value = $state("");
  let data = $state<SearchData | null>(null);
  let failed = $state("");
  let loading = $state(false);

  $effect(() => {
    value = query;
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const text = value.trim();
    navigate(searchHref(text, meaning));
  }

  async function load(text: string, byMeaning: boolean, signal: AbortSignal) {
    loading = true;
    failed = "";
    try {
      data = await getJSON<SearchData>(`/api/search?q=${encodeURIComponent(text)}${byMeaning ? "&mode=meaning" : ""}`, signal);
    } catch (error) {
      if (signal.aborted) return;
      failed = error instanceof Error ? error.message : "Unknown error";
    } finally {
      if (!signal.aborted) loading = false;
    }
  }

  $effect(() => {
    const text = query;
    const byMeaning = meaning;
    if (!text) {
      data = null;
      failed = "";
      loading = false;
      return;
    }
    const controller = new AbortController();
    load(text, byMeaning, controller.signal);
    return () => controller.abort();
  });
</script>

<PageHeader kicker="Everything" title={search.label} subtitle={search.blurb} />

<form role="search" onsubmit={submit} class="mb-4 flex gap-2">
  <label for="search-page-query" class="sr-only">Search Dom</label>
  <Input id="search-page-query" type="search" bind:value maxlength={200} placeholder="Search chats, memory, work…" class="min-w-0 flex-1" />
  <MeaningToggle pressed={meaning} onclick={() => navigate(value.trim() ? searchHref(value, !meaning) : `${search.href}${meaning ? "" : "?mode=meaning"}`)} />
  <Button type="submit">Search</Button>
</form>

<p class="mb-5 text-sm text-muted-foreground" aria-live="polite">
  {#if !query}Type a few words above to look through Dom.{:else if loading}Searching for “{query}”…{:else if data}{meaning ? "Closest in meaning to" : "Results for"} “{data.query}”.{/if}
</p>

{#if failed}
  <StatusNote tone="error" title="Couldn't search" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="outline" onclick={() => load(query, meaning, new AbortController().signal)}>Try again</Button>
  </StatusNote>
{:else if query && data && !loading && data.groups.length === 0}
  <StatusNote
    title="Nothing found"
    detail={meaning
      ? "Nothing close in meaning. If the meaning index isn't set up yet, try Keyword search."
      : "Try fewer or different words, or search by meaning. Every word you type has to appear in the same item."}
  />
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
