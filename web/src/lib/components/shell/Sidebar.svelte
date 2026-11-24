<script lang="ts">
  import PanelLeftClose from "@lucide/svelte/icons/panel-left-close";
  import PanelLeftOpen from "@lucide/svelte/icons/panel-left-open";
  import type { Component } from "svelte";
  import Orbit from "$lib/components/Orbit.svelte";
  import * as Tooltip from "$lib/components/ui/tooltip";
  import { destinations, workspace, type Destination } from "$lib/nav";
  import { shell } from "$lib/shell.svelte";
  import { sidebar, typing } from "$lib/sidebar.svelte";
  import { cn } from "$lib/utils";

  let { path, current }: { path: string; current: Destination } = $props();

  const folded = $derived(sidebar.collapsed);
  const item =
    "group flex h-10 items-center gap-3 rounded-[0.7rem] border border-transparent px-3 text-[0.94rem] text-muted-foreground transition-colors hover:bg-raised hover:text-foreground";
  const active = "border-signal/35 bg-tint text-foreground hover:bg-tint";

  // "[" folds and unfolds the sidebar, unless a text field has the key or a modifier makes it something else.
  function shortcut(event: KeyboardEvent) {
    if (event.key !== "[" || event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented || typing(event.target)) return;
    if (!matchMedia("(min-width: 1024px)").matches) return;
    event.preventDefault();
    sidebar.toggle();
  }
</script>

<svelte:window onkeydown={shortcut} />

{#snippet link(href: string, label: string, Icon: Component, here: boolean, aria: "page" | "location" | undefined, small: boolean, tag?: string)}
  {@const classes = cn(item, small && "h-9", folded && "w-10 justify-center px-0", here && active)}
  {@const icon = cn(small ? "size-4" : "size-[1.15rem]", "shrink-0", here && "text-signal")}
  {#if folded}
    <!-- Folded: the icon alone, named for screen readers, with its label as a tooltip. -->
    <Tooltip.Root>
      <Tooltip.Trigger>
        {#snippet child({ props })}
          <a {...props} {href} class={classes} aria-current={aria} aria-label={tag ? `${label}, in ${tag}` : label}>
            <Icon class={icon} aria-hidden="true" />
          </a>
        {/snippet}
      </Tooltip.Trigger>
      <Tooltip.Content side="right">{label}</Tooltip.Content>
    </Tooltip.Root>
  {:else}
    <a {href} class={classes} aria-current={aria}>
      <Icon class={icon} aria-hidden="true" />
      <span class="flex-1 truncate">{label}</span>
      {#if tag}<span class="font-mono text-[0.62rem] tracking-[0.12em] uppercase text-muted-foreground" aria-label={`in ${tag}`}>{tag}</span>{/if}
    </a>
  {/if}
{/snippet}

<!-- Desktop only: the kit's grouped sidebar, folding to a rail of icons. Control surface is the five destinations of
     the phone menu; Workspace reaches the utility pages, each of which also lives as a tab in Work or System. -->
<div class="hidden border-r bg-rail lg:block">
<aside class={cn("sticky top-0 flex h-dvh flex-col overflow-x-hidden pt-6 pb-5", folded ? "items-center px-2" : "px-4")}>
  <div class={cn("flex items-center", folded ? "flex-col gap-3" : "justify-between gap-2")}>
    <a href="/app" class="flex items-center gap-3 rounded-lg px-1.5 py-1" aria-label={folded ? "Dom, home" : undefined}>
      <Orbit size="sm" activity="idle" />
      {#if !folded}
        <span class="flex flex-col leading-none">
          <span class="text-[1.35rem] font-semibold tracking-tight">dom<span class="text-signal">.</span></span>
          <span class="kicker mt-1.5 text-[0.62rem]">Quiet orbit</span>
        </span>
      {/if}
    </a>
    <Tooltip.Root>
      <Tooltip.Trigger>
        {#snippet child({ props })}
          <button
            {...props}
            type="button"
            class="grid size-9 shrink-0 place-items-center rounded-[0.6rem] text-muted-foreground transition-colors hover:bg-raised hover:text-foreground"
            aria-expanded={!folded}
            aria-controls="sidebar-nav"
            aria-label={folded ? "Expand sidebar" : "Collapse sidebar"}
            aria-keyshortcuts="["
            onclick={() => sidebar.toggle()}
          >
            {#if folded}<PanelLeftOpen class="size-[1.05rem]" aria-hidden="true" />{:else}<PanelLeftClose class="size-[1.05rem]" aria-hidden="true" />{/if}
          </button>
        {/snippet}
      </Tooltip.Trigger>
      <Tooltip.Content side="right">{folded ? "Expand sidebar" : "Collapse sidebar"} <kbd class="ml-1 font-mono opacity-70">[</kbd></Tooltip.Content>
    </Tooltip.Root>
  </div>

  <nav id="sidebar-nav" aria-label="Main" class={cn("flex flex-1 flex-col", folded ? "mt-6" : "mt-8")}>
    <h2 id="nav-control" class={cn("kicker mb-2 px-3", folded && "sr-only")}>Control surface</h2>
    <ul aria-labelledby="nav-control" class="flex flex-col gap-1">
      {#each destinations as destination (destination.id)}
        {@const here = destination.id === current.id}
        <li>{@render link(destination.href, destination.label, destination.icon, here, path === destination.href ? "page" : here ? "location" : undefined, false)}</li>
      {/each}
    </ul>

    <h2 id="nav-workspace" class={cn("kicker mt-auto mb-2 px-3 pt-8", folded && "sr-only")}>Workspace</h2>
    {#if folded}<div class="mt-auto mb-3 h-px w-6 self-center bg-line" aria-hidden="true"></div>{/if}
    <ul aria-labelledby="nav-workspace" class="flex flex-col gap-0.5">
      {#each workspace as { tab, home } (tab.id)}
        {@const here = path === tab.href}
        <li>{@render link(tab.href, tab.label, tab.icon, here, here ? "page" : undefined, true, home)}</li>
      {/each}
    </ul>
  </nav>

  {#if shell.version}
    {#if folded}
      <Tooltip.Root>
        <Tooltip.Trigger>
          {#snippet child({ props })}
            <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
            <p {...props} tabindex="0" class="mt-4 rounded-sm font-mono text-[0.68rem] text-muted-foreground" aria-label={`Dom ${shell.version}`}>{shell.version.replace(/^v/, "").split(/[-+ ]/)[0]}</p>
          {/snippet}
        </Tooltip.Trigger>
        <Tooltip.Content side="right">Dom {shell.version}</Tooltip.Content>
      </Tooltip.Root>
    {:else}
      <p class="mt-4 px-3 font-mono text-[0.68rem] text-muted-foreground">Dom {shell.version}</p>
    {/if}
  {/if}
</aside>
</div>
