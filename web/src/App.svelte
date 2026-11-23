<script lang="ts">
  import BottomBar from "$lib/components/shell/BottomBar.svelte";
  import Sidebar from "$lib/components/shell/Sidebar.svelte";
  import TopBar from "$lib/components/shell/TopBar.svelte";
  import Toaster from "$lib/components/Toaster.svelte";
  import * as Tooltip from "$lib/components/ui/tooltip";
  import { gallery, locate } from "$lib/nav";
  import { interceptLinks, route } from "$lib/router.svelte";
  import Gallery from "./routes/Gallery.svelte";
  import Home from "./routes/Home.svelte";
  import Section from "./routes/Section.svelte";
  import Settings from "./routes/Settings.svelte";

  const isGallery = $derived(route.path === gallery.href);
  const where = $derived(locate(route.path));

  $effect(() => {
    const page = isGallery ? gallery.label : (where.tab?.label ?? where.destination.label);
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
  <div class="min-h-dvh lg:grid lg:grid-cols-[15.5rem_minmax(0,1fr)]">
    <Sidebar path={route.path} current={isGallery ? { ...where.destination, id: "" } : where.destination} />
    <div class="min-w-0">
      <TopBar destination={where.destination} tab={where.tab} title={isGallery ? gallery.label : undefined} />
      <main id="main" tabindex="-1" class="mx-auto max-w-[68rem] px-4 pt-3 pb-28 outline-none sm:px-6 lg:px-9 lg:pt-1 lg:pb-14">
        {#if isGallery}
          <Gallery />
        {:else if where.destination.id === "home"}
          <Home />
        {:else if where.tab?.id === "settings"}
          <Settings destination={where.destination} tab={where.tab} />
        {:else}
          {#key route.path}<Section destination={where.destination} tab={where.tab} />{/key}
        {/if}
      </main>
    </div>
  </div>
  <Toaster />
  <BottomBar path={route.path} current={isGallery ? { ...where.destination, id: "" } : where.destination} />
</Tooltip.Provider>
