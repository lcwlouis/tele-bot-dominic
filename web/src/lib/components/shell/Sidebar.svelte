<script lang="ts">
  import Inbox from "@lucide/svelte/icons/inbox";
  import PanelLeftClose from "@lucide/svelte/icons/panel-left-close";
  import PanelLeftOpen from "@lucide/svelte/icons/panel-left-open";
  import type { Component } from "svelte";
  import Orbit from "$lib/components/Orbit.svelte";
  import * as Tooltip from "$lib/components/ui/tooltip";
  import { destinations, waiting, type Destination } from "$lib/nav";
  import { shell } from "$lib/shell.svelte";
  import { sidebar, typing } from "$lib/sidebar.svelte";
  import { cn, tipTrigger } from "$lib/utils";

  let { path, current }: { path: string; current: Destination } = $props();

  // Folded to a rail, the sidebar opens for a look while a mouse rests on it: over the page, which keeps its
  // width. Clicking the logo spot pins it open. Touch keeps tap behaviour (no hover), and keyboards get each
  // rail icon's tooltip, the logo button and "[".
  let peek = $state(false);
  // While a mouse is over the rail it peeks open, so the rail's own tooltips are for keyboard focus only.
  let hovering = $state(false);
  let timer: ReturnType<typeof setTimeout> | undefined;
  const compact = $derived(sidebar.collapsed && !peek);

  function enter(event: PointerEvent) {
    if (event.pointerType !== "mouse") return;
    hovering = true;
    if (!sidebar.collapsed) return;
    clearTimeout(timer);
    timer = setTimeout(() => (peek = true), 140);
  }
  function leave() {
    clearTimeout(timer);
    hovering = false;
    peek = false;
  }
  function toggle() {
    clearTimeout(timer);
    peek = false;
    sidebar.toggle();
  }

  const item =
    "group flex h-10 items-center gap-3 rounded-[0.7rem] border border-transparent px-3 text-[0.94rem] text-muted-foreground transition-colors duration-(--motion-quick) hover:bg-raised hover:text-foreground";
  const active = "border-signal/35 bg-tint text-foreground hover:bg-tint";

  // "[" folds and unfolds the sidebar, unless a text field has the key or a modifier makes it something else.
  // The links stay the same elements in both layouts, so keyboard focus stays where it was.
  function shortcut(event: KeyboardEvent) {
    if (event.key !== "[" || event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented || typing(event.target)) return;
    if (!matchMedia("(min-width: 1024px)").matches) return;
    event.preventDefault();
    toggle();
  }
  $effect(() => () => clearTimeout(timer));
</script>

<svelte:window onkeydown={shortcut} />

{#snippet link(href: string, label: string, Icon: Component, here: boolean, aria: "page" | "location" | undefined, count?: number)}
  <!-- One link in both layouts. Folded, it is the icon alone, named for screen readers, with its label as a tooltip. -->
  <Tooltip.Root disabled={!compact || hovering}>
    <Tooltip.Trigger>
      {#snippet child({ props })}
        <a
          {...tipTrigger(props)}
          {href}
          class={cn(item, compact && "relative w-10 justify-center px-0", here && active)}
          aria-current={aria}
          aria-label={compact ? (count !== undefined ? `${label}, ${count}` : label) : undefined}
        >
          <Icon class={cn("size-[1.15rem] shrink-0", here && "text-signal")} aria-hidden="true" />
          {#if !compact}
            <span class="flex-1 truncate">{label}</span>
            {#if count !== undefined}
              <span class={cn("rounded-full px-2 py-0.5 font-mono text-xs", count ? "bg-signal text-primary-foreground" : "bg-raised text-muted-foreground")}>
                {count}<span class="sr-only"> waiting</span>
              </span>
            {/if}
          {:else if count}
            <span class="absolute top-1.5 right-1.5 size-2 rounded-full bg-signal ring-2 ring-rail" aria-hidden="true"></span>
          {/if}
        </a>
      {/snippet}
    </Tooltip.Trigger>
    <Tooltip.Content side="right">{label}</Tooltip.Content>
  </Tooltip.Root>
{/snippet}

<!-- Desktop only: the kit's sidebar, folding to a rail of icons. Control surface is the six destinations of the phone
     menu; their own pages are the tabs under each page's header, so the sidebar doesn't repeat them. Waiting for you
     sits at the foot, with its count, one click from anywhere. -->
<div class="relative z-40 hidden border-r bg-rail lg:block">
<aside
  class={cn(
    "sticky top-0 flex h-dvh flex-col overflow-x-hidden bg-rail pt-6 pb-5 transition-[width,box-shadow] duration-(--motion-calm) ease-calm",
    compact ? "w-full items-center px-2" : "px-4",
    peek ? "w-[15.5rem] border-r shadow-[18px_0_40px_-24px_var(--shadow)]" : "w-full",
  )}
  data-peek={peek || undefined}
  onpointerenter={enter}
  onpointerleave={leave}
>
  <div class="flex items-center gap-2">
    <!-- The logo spot is the fold button: the orbit at rest, the panel glyph on hover or keyboard focus. -->
    <Tooltip.Root>
      <Tooltip.Trigger>
        {#snippet child({ props })}
          <!-- Its tooltip adds only the "[" key (aria-keyshortcuts) to the name, except on a peek, where it says
               what a click does. -->
          <button
            {...tipTrigger(props, { onclick: toggle }, !peek)}
            type="button"
            class="group/logo grid size-10 shrink-0 place-items-center rounded-[0.7rem] text-muted-foreground transition-colors duration-(--motion-quick) hover:bg-raised hover:text-foreground"
            aria-expanded={!sidebar.collapsed}
            aria-controls="sidebar-nav"
            aria-label={sidebar.collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-keyshortcuts="["
            data-sidebar-toggle
          >
            <span class="group-hover/logo:hidden group-focus-visible/logo:hidden" aria-hidden="true"><Orbit size="sm" activity="idle" /></span>
            <span class="hidden group-hover/logo:block group-focus-visible/logo:block" aria-hidden="true">
              {#if sidebar.collapsed}<PanelLeftOpen class="size-[1.15rem]" />{:else}<PanelLeftClose class="size-[1.15rem]" />{/if}
            </span>
          </button>
        {/snippet}
      </Tooltip.Trigger>
      <Tooltip.Content side="right">
        {sidebar.collapsed ? (peek ? "Keep the sidebar open" : "Expand sidebar") : "Collapse sidebar"} <kbd class="ml-1 font-mono opacity-70">[</kbd>
      </Tooltip.Content>
    </Tooltip.Root>
    {#if !compact}
      <a href="/app" class="flex min-w-0 items-center rounded-lg px-1 py-1 leading-none">
        <span class="text-[1.35rem] font-semibold tracking-tight">dom<span class="text-signal">.</span></span>
      </a>
    {/if}
  </div>

  <nav id="sidebar-nav" aria-label="Main" class={cn("flex w-full flex-1 flex-col", compact ? "mt-6 items-center" : "mt-8")}>
    <h2 id="nav-control" class={cn("kicker mb-2 px-3", compact && "sr-only")}>Control surface</h2>
    <ul aria-labelledby="nav-control" class={cn("flex flex-col gap-1", !compact && "w-full")}>
      {#each destinations as destination (destination.id)}
        {@const here = destination.id === current.id && path !== waiting.href}
        <li>{@render link(destination.href, destination.label, destination.icon, here, path === destination.href ? "page" : here ? "location" : undefined)}</li>
      {/each}
    </ul>

    <div class={cn("mt-auto pt-8", !compact && "w-full")}>
      {#if compact}<div class="mx-auto mb-3 h-px w-6 bg-line" aria-hidden="true"></div>{/if}
      {@render link(waiting.href, waiting.label, Inbox, path === waiting.href, path === waiting.href ? "page" : undefined, shell.waiting)}
    </div>
  </nav>

  {#if shell.version}
    <Tooltip.Root disabled={!compact}>
      <Tooltip.Trigger>
        {#snippet child({ props })}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
          <p
            {...tipTrigger(props)}
            tabindex={compact ? 0 : undefined}
            class={cn("mt-4 rounded-sm font-mono text-[0.68rem] text-muted-foreground", !compact && "px-3")}
            aria-label={compact ? `Dom ${shell.version}` : undefined}
          >
            {compact ? shell.version.replace(/^v/, "").split(/[-+ ]/)[0] : `Dom ${shell.version}`}
          </p>
        {/snippet}
      </Tooltip.Trigger>
      <Tooltip.Content side="right">Dom {shell.version}</Tooltip.Content>
    </Tooltip.Root>
  {/if}
</aside>
</div>
