<script lang="ts">
  import SearchIcon from "@lucide/svelte/icons/search";
  import { buttonVariants } from "$lib/components/ui/button";
  import { Input } from "$lib/components/ui/input";
  import SearchMode from "./SearchMode.svelte";
  import { search, searchHref } from "$lib/nav";
  import { navigate, route } from "$lib/router.svelte";
  import { searchMode } from "$lib/search-mode.svelte";
  import { cn } from "$lib/utils";

  // Desktop: the search box in the top row. Phone: the menu carries the Search link, so only the desktop box shows.
  let value = $state(new URLSearchParams(route.search).get("q") ?? "");
  // Words by default; "Meaning" asks the embedding index instead (D19). On the results page the address decides.
  let meaning = $state(searchMode.meaning);

  $effect(() => {
    if (route.path !== search.href) {
      meaning = searchMode.meaning;
      return;
    }
    const params = new URLSearchParams(route.search);
    value = params.get("q") ?? "";
    meaning = params.has("q") ? params.get("mode") === "meaning" : searchMode.meaning;
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    navigate(searchHref(value, meaning));
  }

  function change(next: boolean) {
    meaning = next;
    searchMode.set(next);
    // On the results page a new mode searches again at once; elsewhere it waits for Enter.
    if (route.path === search.href && value.trim()) navigate(searchHref(value, meaning));
  }
</script>

<form role="search" onsubmit={submit} class="relative hidden lg:block">
  <label for="global-search" class="sr-only">Search Dom</label>
  <SearchIcon class="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
  <Input id="global-search" type="search" bind:value maxlength={200} placeholder="Search chats, memory, work…" class="w-[26rem] pr-36 pl-9" />
  <SearchMode {meaning} onchange={change} class="absolute top-1/2 right-1.5 -translate-y-1/2" />
</form>
<a
  href={search.href}
  class={cn(buttonVariants({ variant: "outline", size: "icon" }), "lg:hidden")}
  aria-label="Search"
>
  <SearchIcon aria-hidden="true" />
</a>
