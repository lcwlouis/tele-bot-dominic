<script lang="ts">
  import { getJSON, type SearchData, type SearchHit } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { search, searchHref } from "$lib/nav";
  import SearchMode from "$lib/components/shell/SearchMode.svelte";
  import { navigate, route } from "$lib/router.svelte";
  import { searchMode } from "$lib/search-mode.svelte";
  import { cn } from "$lib/utils";

  const query = $derived((new URLSearchParams(route.search).get("q") ?? "").trim());
  // A query's address carries its mode; with no query yet, the mode the owner last picked.
  const meaning = $derived.by(() => {
    const params = new URLSearchParams(route.search);
    return params.has("q") ? params.get("mode") === "meaning" : searchMode.meaning;
  });
  let value = $state("");
  let data = $state<SearchData | null>(null);
  let failed = $state("");
  let loading = $state(false);

  $effect(() => {
    value = query;
  });

  // Ranked hits first: each group by score, and the groups by their best hit. Hits without a score keep the server's
  // order after the scored ones (Array.prototype.sort is stable).
  const rank = (score: number | null | undefined) => (typeof score === "number" && Number.isFinite(score) ? score : -Infinity);
  const higher = (a: number, b: number) => (a === b ? 0 : a > b ? -1 : 1);
  const best = (hits: SearchHit[]) => Math.max(...hits.map((hit) => rank(hit.score)));
  const groups = $derived(
    (data?.groups ?? [])
      .map((group) => ({ ...group, hits: [...group.hits].sort((a, b) => higher(rank(a.score), rank(b.score))) }))
      .sort((a, b) => higher(best(a.hits), best(b.hits))),
  );
  const STRENGTHS = new Set(["strong", "good", "weak"]);
  const strength = (hit: SearchHit) => (hit.match && STRENGTHS.has(hit.match) ? hit.match : null);

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
  <div class="relative min-w-0 flex-1">
    <Input id="search-page-query" type="search" bind:value maxlength={200} placeholder="Search chats, memory, work…" class="pr-36" />
    <SearchMode
      {meaning}
      onchange={(next) => (searchMode.set(next), navigate(searchHref(value, next)))}
      class="absolute top-1/2 right-1.5 -translate-y-1/2"
    />
  </div>
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
      ? "Nothing close in meaning. If the meaning index isn't set up yet, search by words."
      : "Try fewer or different words, or search by meaning. Every word you type has to appear in the same item."}
  />
{:else if data && query}
  <div class="grid gap-5">
    {#each groups as group (group.kind)}
      <section aria-labelledby={`search-${group.kind}`}>
        <Card.Root>
          <Card.Header>
            <Card.Title id={`search-${group.kind}`} class="text-lg">{group.label}</Card.Title>
          </Card.Header>
          <Card.Content>
            <ul class="grid gap-1">
              {#each group.hits as hit}
                {@const how = strength(hit)}
                <li>
                  <a href={hit.href} class="block rounded-[0.6rem] px-2.5 py-2 transition-colors outline-none hover:bg-raised focus-visible:ring-3 focus-visible:ring-ring/50">
                    <span class="flex flex-wrap items-center gap-2">
                      <span class="min-w-0 break-words font-medium">{hit.title}</span>
                      {#if !hit.full}<Badge variant="secondary">Preview</Badge>{/if}
                      {#if how}<span class={cn("ml-auto text-caption", how === "strong" ? "text-signal-strong" : "text-muted-foreground")}>{how} match</span>{/if}
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
