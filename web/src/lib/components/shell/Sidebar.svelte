<script lang="ts">
  import Orbit from "$lib/components/Orbit.svelte";
  import { destinations, workspace, type Destination } from "$lib/nav";
  import { cn } from "$lib/utils";

  let { path, current }: { path: string; current: Destination } = $props();

  const item =
    "group flex h-10 items-center gap-3 rounded-[0.7rem] border border-transparent px-3 text-[0.94rem] text-muted-foreground transition-colors hover:bg-raised hover:text-foreground";
  const active = "border-signal/35 bg-tint text-foreground hover:bg-tint";
</script>

<!-- Desktop only: the kit's grouped sidebar. Control surface is the five destinations of the phone's bar;
     Workspace reaches the utility pages, each of which also lives as a tab in Work or System. -->
<div class="hidden border-r bg-rail lg:block">
<aside class="sticky top-0 flex h-dvh flex-col px-4 pt-6 pb-5">
  <a href="/app" class="flex items-center gap-3 rounded-lg px-1.5 py-1">
    <Orbit size="sm" activity="idle" />
    <span class="flex flex-col leading-none">
      <span class="text-[1.35rem] font-semibold tracking-tight">dom<span class="text-signal">.</span></span>
      <span class="kicker mt-1.5 text-[0.62rem]">Quiet orbit</span>
    </span>
  </a>

  <nav aria-label="Main" class="mt-8 flex flex-1 flex-col">
    <h2 id="nav-control" class="kicker mb-2 px-3">Control surface</h2>
    <ul aria-labelledby="nav-control" class="flex flex-col gap-1">
      {#each destinations as destination (destination.id)}
        {@const here = destination.id === current.id}
        <li>
          <a
            href={destination.href}
            class={cn(item, here && active)}
            aria-current={path === destination.href ? "page" : here ? "location" : undefined}
          >
            <destination.icon class={cn("size-[1.15rem]", here && "text-signal")} aria-hidden="true" />
            {destination.label}
          </a>
        </li>
      {/each}
    </ul>

    <h2 id="nav-workspace" class="kicker mt-auto mb-2 px-3 pt-8">Workspace</h2>
    <ul aria-labelledby="nav-workspace" class="flex flex-col gap-0.5">
      {#each workspace as { tab, home } (tab.id)}
        {@const here = path === tab.href}
        <li>
          <a href={tab.href} class={cn(item, "h-9", here && active)} aria-current={here ? "page" : undefined}>
            <tab.icon class={cn("size-4", here && "text-signal")} aria-hidden="true" />
            <span class="flex-1">{tab.label}</span>
            <span class="font-mono text-[0.62rem] tracking-[0.12em] uppercase text-muted-foreground" aria-label={`in ${home}`}>{home}</span>
          </a>
        </li>
      {/each}
    </ul>
  </nav>

  <div class="mt-5 border-t pt-4 px-1.5">
    <a href="/" class="text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">
      Current dashboard
    </a>
  </div>
</aside>
</div>
