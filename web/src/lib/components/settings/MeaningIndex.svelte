<script lang="ts">
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { onDestroy } from "svelte";
  import { getJSON, send, type IndexEstimate, type IndexProgress } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Progress } from "$lib/components/ui/progress";
  import { toast } from "$lib/toast.svelte";

  // The meaning-search index (D19): a new embedding model is staged when it is saved, and builds only after the
  // owner confirms here. The serving index keeps answering until the new one is complete. `version` moves when the
  // embedding role is saved, so a freshly staged model shows at once.
  let { version = "" }: { version?: string } = $props();

  const POLL_MS = 3000;
  let estimate = $state<IndexEstimate | null>(null);
  let progress = $state<IndexProgress | null>(null);
  let failed = $state("");
  let busy = $state<"" | "confirm" | "cancel">("");
  let timer: ReturnType<typeof setTimeout> | undefined;

  async function load() {
    clearTimeout(timer);
    try {
      [estimate, progress] = await Promise.all([
        getJSON<IndexEstimate>("/api/memory/index/estimate"),
        getJSON<IndexProgress>("/api/memory/index/progress"),
      ]);
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
    // Follow a running build closely; otherwise look again only when the role changes.
    if (progress?.state === "running") timer = setTimeout(load, POLL_MS);
  }

  onDestroy(() => clearTimeout(timer));
  $effect(() => {
    void version;
    load();
  });

  async function act(action: "confirm" | "cancel") {
    busy = action;
    try {
      await send(`/api/memory/index/${action}`, {});
      toast.show("ok", action === "confirm" ? "Reindex started." : "Reindex cancelled.");
    } catch {
      // the toast says why
    } finally {
      busy = "";
      await load();
    }
  }

  const minutes = (seconds: number) => (seconds < 90 ? `${Math.max(1, Math.round(seconds))} seconds` : `${Math.round(seconds / 60)} minutes`);
  const STATE: Record<string, string> = { running: "Running", paused: "Paused", idle: "Idle" };
</script>

<Card.Root role="region" aria-labelledby="meaning-index-title">
  <Card.Header>
    <div class="flex items-center gap-1.5">
      <Card.Title id="meaning-index-title" class="text-lg">Meaning index</Card.Title>
      <InfoGlyph label="About the meaning index">
        Search by meaning compares vectors of your notes, journal and messages. Changing the embedding model rebuilds
        them; until then the current index keeps answering.
      </InfoGlyph>
    </div>
  </Card.Header>
  <Card.Content class="flex flex-col gap-4">
    {#if failed && !progress}
      <StatusNote tone="error" title="Couldn't load the index" detail={`Dom's dashboard answered: ${failed}.`}>
        <Button variant="secondary" onclick={load}>Retry</Button>
      </StatusNote>
    {:else if !progress || !estimate}
      <p role="status" class="text-sm text-muted-foreground">Loading…</p>
    {:else}
      {@const total = progress.done + progress.left}
      <dl class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
        <div><dt class="text-muted-foreground">State</dt><dd class="font-medium">{STATE[progress.state] ?? progress.state}</dd></div>
        <div><dt class="text-muted-foreground">Model</dt><dd class="font-mono break-all">{progress.model || "None"}</dd></div>
        <div><dt class="text-muted-foreground">Done</dt><dd class="font-mono">{progress.done}</dd></div>
        <div><dt class="text-muted-foreground">Left</dt><dd class="font-mono">{progress.left}</dd></div>
      </dl>
      {#if total > 0}
        <Progress value={progress.done} max={total} aria-label="Items indexed" />
      {/if}
      {#if progress.last_error}
        <p role="alert" class="text-sm text-coral">Last error: {progress.last_error}</p>
      {/if}
      {#if progress.stale}
        <p class="text-sm text-coral">The model's answers drifted since the index was built: rebuild it to search by meaning again.</p>
      {/if}
      {#if estimate.staged}
        <div class="rounded-xl border bg-raised/40 p-3.5">
          <p id="meaning-staged-title" class="font-medium">
            {estimate.rebuild ? "Rebuild" : "New model"}: <span class="font-mono break-all">{estimate.model}</span>
          </p>
          <p class="mt-1 text-sm text-muted-foreground">
            {estimate.items} items, about {minutes(estimate.rough_seconds)}. Cloud models charge for each item.
          </p>
          <div class="mt-3 flex flex-wrap gap-2">
            <Button onclick={() => act("confirm")} disabled={busy !== "" || progress.state === "running"}>
              {#if busy === "confirm"}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" />{/if}
              Confirm reindex
            </Button>
            {#if progress.state !== "running"}
              <Button variant="outline" onclick={() => act("cancel")} disabled={busy !== ""}>Cancel <span class="sr-only">the staged model</span></Button>
            {/if}
          </div>
        </div>
      {/if}
      {#if progress.state === "running"}
        <div>
          <Button variant="outline" onclick={() => act("cancel")} disabled={busy !== ""}>
            {#if busy === "cancel"}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" />{/if}
            Cancel reindex
          </Button>
        </div>
      {:else if !estimate.staged && !progress.model}
        <p class="text-sm text-muted-foreground">Set up the embedding model above to search by meaning.</p>
      {/if}
    {/if}
  </Card.Content>
</Card.Root>
