<script lang="ts">
  import { getJSON, type TracePart } from "$lib/api";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Button } from "$lib/components/ui/button";
  import TraceView from "./TraceView.svelte";

  // Fetches one turn's trace (or one background part) and shows it. Kept apart from TraceView so the Talk page loads
  // a turn's trace only when its card is opened.
  let { turnId = null, partId = null }: { turnId?: number | null; partId?: number | null } = $props();

  let parts = $state<TracePart[] | null>(null);
  let failed = $state("");

  async function load() {
    failed = "";
    try {
      const path = turnId !== null ? `/api/traces/turn/${turnId}` : `/api/traces/part/${partId}`;
      parts = (await getJSON<{ parts: TracePart[] }>(path)).parts;
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  $effect(() => {
    void turnId;
    void partId;
    parts = null;
    load();
  });
</script>

{#if failed}
  <StatusNote tone="error" title="Couldn't load the trace" detail={failed.startsWith("404") ? "It is older than the trace retention, or ran before tracing." : `Dom's dashboard answered: ${failed}.`}>
    <Button variant="outline" onclick={load}>Try again</Button>
  </StatusNote>
{:else if parts === null}
  <div class="h-24 animate-pulse rounded-lg bg-raised" aria-hidden="true"></div>
  <span class="sr-only" role="status">Loading the trace…</span>
{:else}
  <TraceView {parts} turnId={turnId} />
{/if}
