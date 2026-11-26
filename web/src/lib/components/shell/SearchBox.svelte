<script lang="ts">
  import SearchIcon from "@lucide/svelte/icons/search";
  import { buttonVariants } from "$lib/components/ui/button";
  import { Input } from "$lib/components/ui/input";
  import MeaningToggle from "./MeaningToggle.svelte";
  import { search, searchHref } from "$lib/nav";
  import { navigate, route } from "$lib/router.svelte";
  import { cn } from "$lib/utils";

  // Desktop: the search box in the top row. Phone: the menu carries the Search link, so only the desktop box shows.
  let value = $state(new URLSearchParams(route.search).get("q") ?? "");
  // Keyword by default; "Meaning" asks the embedding index instead (D19).
  let meaning = $state(new URLSearchParams(route.search).get("mode") === "meaning");

  $effect(() => {
    if (route.path !== search.href) return;
    const params = new URLSearchParams(route.search);
    value = params.get("q") ?? "";
    meaning = params.get("mode") === "meaning";
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    navigate(searchHref(value, meaning));
  }

  function toggle() {
    meaning = !meaning;
    // On the results page the toggle searches again at once; elsewhere it waits for Enter.
    if (route.path === search.href && value.trim()) navigate(searchHref(value, meaning));
  }
</script>

<form role="search" onsubmit={submit} class="relative hidden lg:block">
  <label for="global-search" class="sr-only">Search Dom</label>
  <SearchIcon class="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
  <div class="flex items-center gap-1.5">
    <Input id="global-search" type="search" bind:value maxlength={200} placeholder="Search chats, memory, work…" class={cn("w-72 pl-9")} />
    <MeaningToggle pressed={meaning} onclick={toggle} />
  </div>
</form>
<a
  href={search.href}
  class={cn(buttonVariants({ variant: "outline", size: "icon" }), "lg:hidden")}
  aria-label="Search"
>
  <SearchIcon aria-hidden="true" />
</a>
