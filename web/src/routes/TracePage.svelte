<script lang="ts">
  import ArrowLeft from "@lucide/svelte/icons/arrow-left";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import TraceLoader from "$lib/components/trace/TraceLoader.svelte";
  import { buttonVariants } from "$lib/components/ui/button";
  import type { Destination, Tab } from "$lib/nav";

  // /app/system/turns/<id>: one turn's full trace on its own page, for any chat (the Turn log links here).
  let { destination, tab, turnId }: { destination: Destination; tab: Tab; turnId: number } = $props();
</script>

<PageHeader kicker={`${destination.label} · ${tab.label}`} title={`Turn ${turnId}`} subtitle="Everything this turn did, as the debug tracer recorded it, secrets redacted.">
  {#snippet aside()}
    <a href={tab.href} class={buttonVariants({ variant: "outline", size: "sm" })}><ArrowLeft aria-hidden="true" />Turn log</a>
  {/snippet}
</PageHeader>

<div class="rounded-xl border bg-surface p-4">
  {#key turnId}<TraceLoader {turnId} />{/key}
</div>
