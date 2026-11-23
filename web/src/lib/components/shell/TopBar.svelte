<script lang="ts">
  import Moon from "@lucide/svelte/icons/moon";
  import Sun from "@lucide/svelte/icons/sun";
  import Orbit from "$lib/components/Orbit.svelte";
  import { Button } from "$lib/components/ui/button";
  import type { Destination, Tab } from "$lib/nav";
  import { theme } from "$lib/theme.svelte";

  let { destination, tab, title }: { destination: Destination; tab?: Tab; title?: string } = $props();
</script>

<header class="sticky top-0 z-30 border-b border-transparent bg-background/85 backdrop-blur lg:static lg:bg-transparent lg:backdrop-blur-none">
  <div class="mx-auto flex h-16 max-w-[68rem] items-center gap-3 px-4 sm:px-6 lg:h-20 lg:px-9">
    <a href="/app" class="flex items-center gap-2.5 lg:hidden">
      <Orbit size="sm" activity="idle" />
      <span class="text-xl font-semibold tracking-tight">dom<span class="text-signal">.</span></span>
    </a>
    <nav aria-label="Breadcrumb" class="hidden text-sm lg:block">
      <ol class="flex items-center gap-2 text-muted-foreground">
        <li>Dom</li>
        <li aria-hidden="true">/</li>
        {#if title}
          <li class="font-medium text-foreground" aria-current="page">{title}</li>
        {:else if tab && tab.href !== destination.href}
          <li><a href={destination.href} class="hover:text-foreground">{destination.label}</a></li>
          <li aria-hidden="true">/</li>
          <li class="font-medium text-foreground" aria-current="page">{tab.label}</li>
        {:else}
          <li class="font-medium text-foreground" aria-current="page">{destination.label}</li>
        {/if}
      </ol>
    </nav>
    <div class="ml-auto flex items-center gap-2">
      <Button
        variant="outline"
        size="icon"
        onclick={() => theme.toggle()}
        aria-label={theme.value === "dark" ? "Switch to the light theme" : "Switch to the dark theme"}
        title={theme.value === "dark" ? "Light theme" : "Dark theme"}
      >
        {#if theme.value === "dark"}<Sun />{:else}<Moon />{/if}
      </Button>
    </div>
  </div>
</header>
