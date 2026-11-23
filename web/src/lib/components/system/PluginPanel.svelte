<script lang="ts">
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import { onMount } from "svelte";
  import { ApiError, send, type PanelResult } from "$lib/api";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";

  // One approved plugin panel. It loads by itself (a slow plugin never delays the page) and refreshes on request.
  // Everything shown is schema-checked data from the server, printed as text: a plugin never supplies markup.
  let { plugin, id, title }: { plugin: string; id: string; title: string } = $props();

  let result = $state<PanelResult | null>(null);
  let failed = $state("");
  let loading = $state(false);

  async function run() {
    if (loading) return;
    loading = true;
    try {
      result = await send<PanelResult>("/api/system/plugins/panel", { name: plugin, panel_id: id }, { quiet: true });
      failed = "";
    } catch (error) {
      failed = error instanceof ApiError ? error.message : "The panel could not run.";
    } finally {
      loading = false;
    }
  }

  onMount(run);
</script>

<Card.Root>
  <Card.Header>
    <Card.Title class="text-base">{result?.title || title}</Card.Title>
    <Card.Description>From plugin {plugin}</Card.Description>
    <Card.Action>
      <Button variant="ghost" size="sm" disabled={loading} aria-label={`Refresh ${title}`} onclick={run}>
        <RefreshCw aria-hidden="true" /> Refresh
      </Button>
    </Card.Action>
  </Card.Header>
  <Card.Content class="grid gap-3 text-sm" aria-live="polite">
    {#if failed || result?.error}
      <p class="text-coral" role="status">{failed || result?.error}</p>
    {:else if result === null}
      <p class="text-muted-foreground">Loading from plugin {plugin}…</p>
    {:else}
      {#each result.paragraphs as text, index (index)}<p class="break-words whitespace-pre-wrap">{text}</p>{/each}
      {#if result.pairs.length}
        <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1">
          {#each result.pairs as [key, value], index (index)}
            <dt class="text-muted-foreground">{key}</dt><dd class="break-words">{value}</dd>
          {/each}
        </dl>
      {/if}
      {#if result.columns.length}
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-left text-muted-foreground">{#each result.columns as column, index (index)}<th scope="col" class="py-1.5 pr-3 font-medium">{column}</th>{/each}</tr>
            </thead>
            <tbody>
              {#each result.rows as row, rowIndex (rowIndex)}
                <tr class="border-t">{#each row as cell, cellIndex (cellIndex)}<td class="py-1.5 pr-3 break-words">{cell}</td>{/each}</tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    {/if}
  </Card.Content>
</Card.Root>
