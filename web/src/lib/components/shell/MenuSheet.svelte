<script lang="ts">
  import Inbox from "@lucide/svelte/icons/inbox";
  import Menu from "@lucide/svelte/icons/menu";
  import X from "@lucide/svelte/icons/x";
  import { buttonVariants } from "$lib/components/ui/button";
  import * as Sheet from "$lib/components/ui/sheet";
  import { destinations, waiting, type Destination } from "$lib/nav";
  import { shell } from "$lib/shell.svelte";
  import { cn } from "$lib/utils";

  // Phone and tablet: every destination and its tabs, behind one button in the top row. Nothing stays on screen.
  let { path, current }: { path: string; current: Destination } = $props();

  let open = $state(false);
  let navigated = false; // a tap on a link lands focus on the new page's heading, not back on the button

  function go() {
    navigated = true;
    open = false;
  }

  const link =
    "flex min-h-11 items-center gap-3 rounded-[0.7rem] border border-transparent px-3 text-[0.95rem] text-muted-foreground transition-colors hover:bg-raised hover:text-foreground";
  const active = "border-signal/35 bg-tint text-foreground hover:bg-tint";
  const label = $derived(shell.waiting ? `Menu, ${shell.waiting} waiting for you` : "Menu");
</script>

<Sheet.Root bind:open>
  <Sheet.Trigger class={cn(buttonVariants({ variant: "outline", size: "icon" }), "relative lg:hidden")} aria-label={label}>
    <Menu aria-hidden="true" />
    {#if shell.waiting}
      <span class="absolute -top-0.5 -right-0.5 size-2.5 rounded-full bg-signal ring-2 ring-background" aria-hidden="true"></span>
    {/if}
  </Sheet.Trigger>
  <Sheet.Content
    onswipe={() => (open = false)}
    onCloseAutoFocus={(event) => {
      if (!navigated) return;
      navigated = false;
      event.preventDefault();
      document.querySelector<HTMLElement>("main h1")?.focus();
    }}
  >
    <div class="flex items-center justify-between px-4 pt-4 pb-2">
      <Sheet.Title class="text-lg font-semibold tracking-tight">dom<span class="text-signal">.</span> menu</Sheet.Title>
      <Sheet.Close class={buttonVariants({ variant: "ghost", size: "icon" })} aria-label="Close the menu">
        <X aria-hidden="true" />
      </Sheet.Close>
    </div>
    <Sheet.Description class="sr-only">Every page of Dom's dashboard.</Sheet.Description>
    <nav aria-label="Menu" class="no-scrollbar flex-1 overflow-y-auto px-3 pb-[max(1rem,env(safe-area-inset-bottom))]">
      <a
        href={waiting.href}
        onclick={go}
        aria-current={path === waiting.href ? "page" : undefined}
        class={cn(link, "mb-3", path === waiting.href && active)}
      >
        <Inbox class="size-[1.15rem]" aria-hidden="true" />
        <span class="flex-1">{waiting.label}</span>
        <span class={cn("rounded-full px-2 py-0.5 font-mono text-xs", shell.waiting ? "bg-signal text-primary-foreground" : "bg-raised text-muted-foreground")}>
          {shell.waiting}<span class="sr-only"> waiting</span>
        </span>
      </a>
      <ul class="grid gap-1">
        {#each destinations as destination (destination.id)}
          {@const here = destination.id === current.id && path !== waiting.href}
          <li>
            <a
              href={destination.href}
              onclick={go}
              aria-current={path === destination.href && !destination.tabs ? "page" : here ? "location" : undefined}
              class={cn(link, here && !destination.tabs && active, here && destination.tabs && "text-foreground")}
            >
              <destination.icon class={cn("size-[1.15rem]", here && "text-signal")} aria-hidden="true" />
              {destination.label}
            </a>
            {#if destination.tabs}
              <ul class="mt-0.5 mb-1 ml-[1.15rem] grid gap-0.5 border-l pl-3" aria-label={`${destination.label} pages`}>
                {#each destination.tabs as tab (tab.id)}
                  {@const on = path === tab.href || (tab.href !== destination.href && path.startsWith(tab.href + "/"))}
                  <li>
                    <a href={tab.href} onclick={go} aria-current={on ? "page" : undefined} class={cn(link, "min-h-10 text-sm", on && active)}>
                      <tab.icon class={cn("size-4", on && "text-signal")} aria-hidden="true" />
                      {tab.label}
                    </a>
                  </li>
                {/each}
              </ul>
            {/if}
          </li>
        {/each}
      </ul>
    </nav>
  </Sheet.Content>
</Sheet.Root>
