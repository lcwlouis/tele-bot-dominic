<script lang="ts">
  import type { Snippet } from "svelte";

  let { kicker, title, subtitle, aside }: { kicker: string; title: string; subtitle?: string; aside?: Snippet } = $props();
</script>

<div class="mb-7 flex flex-wrap items-end gap-4 lg:mb-8">
  <div class="min-w-0 flex-1">
    <p class="kicker">{kicker}</p>
    <!-- tabindex lets the router move focus here after a page change. -->
    <h1 tabindex="-1" class="mt-2 text-[2rem] leading-tight font-semibold tracking-tight outline-none sm:text-[2.5rem]">{title}</h1>
    {#if subtitle}<p class="mt-2 max-w-2xl text-[0.95rem] text-muted-foreground">{subtitle}</p>{/if}
  </div>
  {#if aside}<div class="flex w-full flex-wrap items-center gap-2 empty:hidden sm:w-auto">{@render aside()}</div>{/if}
</div>
