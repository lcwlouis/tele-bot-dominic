<script lang="ts">
  import SearchIcon from "@lucide/svelte/icons/search";
  import { buttonVariants } from "$lib/components/ui/button";
  import { Input } from "$lib/components/ui/input";
  import { search } from "$lib/nav";
  import { navigate, route } from "$lib/router.svelte";
  import { cn } from "$lib/utils";

  // Desktop: the search box in the top row. Phone: the menu carries the Search link, so only the desktop box shows.
  let value = $state(new URLSearchParams(route.search).get("q") ?? "");

  $effect(() => {
    if (route.path === search.href) value = new URLSearchParams(route.search).get("q") ?? "";
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const text = value.trim();
    navigate(text ? `${search.href}?q=${encodeURIComponent(text)}` : search.href);
  }
</script>

<form role="search" onsubmit={submit} class="relative hidden lg:block">
  <label for="global-search" class="sr-only">Search Dom</label>
  <SearchIcon class="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
  <Input id="global-search" type="search" bind:value maxlength={200} placeholder="Search chats, memory, work…" class={cn("w-72 pl-9")} />
</form>
<a
  href={search.href}
  class={cn(buttonVariants({ variant: "outline", size: "icon" }), "lg:hidden")}
  aria-label="Search"
>
  <SearchIcon aria-hidden="true" />
</a>
