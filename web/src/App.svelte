<script lang="ts">
  import Sidebar from "$lib/components/shell/Sidebar.svelte";
  import TopBar from "$lib/components/shell/TopBar.svelte";
  import Toaster from "$lib/components/Toaster.svelte";
  import * as Tooltip from "$lib/components/ui/tooltip";
  import { gallery, locate, search, secretEntry, waiting } from "$lib/nav";
  import { interceptLinks, route } from "$lib/router.svelte";
  import { shell } from "$lib/shell.svelte";
  import { sidebar } from "$lib/sidebar.svelte";
  import { cn } from "$lib/utils";
  import Access from "./routes/Access.svelte";
  import Gallery from "./routes/Gallery.svelte";
  import Briefs from "./routes/Briefs.svelte";
  import Chats from "./routes/Chats.svelte";
  import Desk from "./routes/Desk.svelte";
  import Home from "./routes/Home.svelte";
  import Memory from "./routes/Memory.svelte";
  import OwnWork from "./routes/OwnWork.svelte";
  import Reminders from "./routes/Reminders.svelte";
  import Plugins from "./routes/Plugins.svelte";
  import PluginReview from "./routes/PluginReview.svelte";
  import Talk from "./routes/Talk.svelte";
  import TracePage from "./routes/TracePage.svelte";
  import Turns from "./routes/Turns.svelte";
  import Usage from "./routes/Usage.svelte";
  import Spend from "./routes/Spend.svelte";
  import Library from "./routes/Library.svelte";
  import Waiting from "./routes/Waiting.svelte";
  import Search from "./routes/Search.svelte";
  import SecretEntry from "./routes/SecretEntry.svelte";
  import Settings from "./routes/Settings.svelte";

  const isGallery = $derived(route.path === gallery.href);
  const isWaiting = $derived(route.path === waiting.href);
  const isSearch = $derived(route.path === search.href);
  const isSecret = $derived(route.path === secretEntry.href);
  const where = $derived(locate(route.path));
  // /app/system/plugins/{name}/{digest}: one plugin version's code review.
  const review = $derived(where.tab?.id === "plugins" ? route.path.slice(where.tab.href.length + 1).split("/") : []);
  // /app/system/turns/{id}: one turn's trace.
  const traced = $derived(where.tab?.id === "turns" ? Number(route.path.slice(where.tab.href.length + 1)) || null : null);

  // The menu's waiting dot: fresh on every page change, and every minute while the tab is in view.
  $effect(() => {
    void route.path;
    shell.refresh();
  });
  $effect(() => {
    const timer = setInterval(() => document.visibilityState === "visible" && shell.refresh(), 60_000);
    return () => clearInterval(timer);
  });

  $effect(() => {
    const page = isGallery ? gallery.label : isWaiting ? waiting.label : isSearch ? search.label : isSecret ? secretEntry.label : (where.tab?.label ?? where.destination.label);
    document.title = page === "Home" ? "Dom" : `${page} · Dom`;
  });
</script>

<svelte:document onclick={interceptLinks} />

<Tooltip.Provider delayDuration={250}>
  <a
    href="#main"
    class="sr-only z-50 rounded-lg bg-primary px-4 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
  >
    Skip to content
  </a>
  <!-- The sidebar folds to a rail; the columns ease between widths, unless the owner asks for less motion. -->
  <div
    class={cn(
      "min-h-dvh lg:grid lg:transition-[grid-template-columns] lg:duration-200 lg:ease-out motion-reduce:transition-none",
      sidebar.collapsed ? "lg:grid-cols-[4.25rem_minmax(0,1fr)]" : "lg:grid-cols-[15.5rem_minmax(0,1fr)]",
    )}
  >
    <Sidebar path={route.path} current={isGallery ? { ...where.destination, id: "" } : where.destination} />
    <div class="min-w-0">
      <TopBar
        destination={where.destination}
        tab={where.tab}
        title={isGallery ? gallery.label : isWaiting ? waiting.label : isSearch ? search.label : isSecret ? secretEntry.label : undefined}
        path={route.path}
        current={isGallery ? { ...where.destination, id: "" } : where.destination}
      />
      <main id="main" tabindex="-1" class="mx-auto max-w-[68rem] px-4 pt-2 pb-10 outline-none sm:px-6 lg:px-9 lg:pt-1 lg:pb-14">
        {#if isGallery}
          <Gallery />
        {:else if isWaiting}
          <Waiting />
        {:else if isSearch}
          <Search />
        {:else if isSecret}
          <SecretEntry />
        {:else if where.destination.id === "home"}
          <Home />
        {:else if where.destination.id === "talk"}
          <Talk />
        {:else if where.destination.id === "chats"}
          <Chats destination={where.destination} />
        {:else if where.destination.id === "memory"}
          <Memory destination={where.destination} />
        {:else if where.tab?.id === "settings"}
          <Settings destination={where.destination} tab={where.tab} />
        {:else if where.tab?.id === "access"}
          <Access destination={where.destination} tab={where.tab} />
        {:else if where.tab?.id === "desk"}
          <Desk destination={where.destination} tab={where.tab} />
        {:else if where.tab?.id === "own-work"}
          <OwnWork destination={where.destination} tab={where.tab} />
        {:else if where.tab?.id === "reminders"}
          <Reminders destination={where.destination} tab={where.tab} />
        {:else if where.tab?.id === "briefs"}
          <Briefs destination={where.destination} tab={where.tab} />
        {:else if where.tab?.id === "plugins" && review.length === 2}
          {#key route.path}<PluginReview destination={where.destination} tab={where.tab} name={review[0]} digest={review[1]} />{/key}
        {:else if where.tab?.id === "plugins"}
          <Plugins destination={where.destination} tab={where.tab} />
        {:else if where.tab?.id === "library"}
          <Library destination={where.destination} tab={where.tab} />
        {:else if where.tab?.id === "spend"}
          <Spend destination={where.destination} tab={where.tab} />
        {:else if where.tab?.id === "usage"}
          <Usage destination={where.destination} tab={where.tab} />
        {:else if where.tab?.id === "turns" && traced !== null}
          <TracePage destination={where.destination} tab={where.tab} turnId={traced} />
        {:else if where.tab?.id === "turns"}
          <Turns destination={where.destination} tab={where.tab} />
        {/if}
      </main>
    </div>
  </div>
  <Toaster />
</Tooltip.Provider>
