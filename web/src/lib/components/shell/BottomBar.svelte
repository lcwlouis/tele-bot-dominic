<script lang="ts">
  import { destinations, type Destination } from "$lib/nav";
  import { cn } from "$lib/utils";

  let { current, path }: { current: Destination; path: string } = $props();
</script>

<!-- Phone and tablet: the same five destinations as the desktop's Control surface. -->
<nav
  aria-label="Main"
  class="fixed inset-x-0 bottom-0 z-40 border-t bg-rail/95 pb-[env(safe-area-inset-bottom)] backdrop-blur lg:hidden"
>
  <ul class="mx-auto grid max-w-xl grid-cols-5 px-1.5 pt-1.5 pb-1.5">
    {#each destinations as destination (destination.id)}
      {@const here = destination.id === current.id}
      <li>
        <a
          href={destination.href}
          aria-current={path === destination.href ? "page" : here ? "location" : undefined}
          class={cn(
            "flex h-14 flex-col items-center justify-center gap-1 rounded-xl text-[0.72rem] font-medium text-muted-foreground transition-colors hover:text-foreground",
            here && "bg-tint text-signal-strong",
          )}
        >
          <destination.icon class="size-5" aria-hidden="true" />
          {destination.label}
        </a>
      </li>
    {/each}
  </ul>
</nav>
