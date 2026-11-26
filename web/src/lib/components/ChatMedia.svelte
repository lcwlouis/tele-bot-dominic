<script lang="ts">
  import { onMount } from "svelte";
  import { getJSON, type MediaStanding } from "$lib/api";
  import { Button } from "$lib/components/ui/button";
  import { KIND_LABELS, standingNote } from "$lib/media-standing";

  // What studio media one chat has used and has left, against the limits of its own scope (your DM, a friend's DM or
  // a group) and its own deliveries. Read from the quota without reserving anything.
  let { chatId, title }: { chatId: number; title: string } = $props();

  let standing = $state<MediaStanding | null>(null);
  let failed = $state("");

  async function load() {
    failed = "";
    try {
      standing = await getJSON<MediaStanding>(`/api/chats/${chatId}/media`);
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }
  onMount(load);
</script>

<section aria-label={`Media used and left in ${title}`} class="grid gap-3 border-t pt-4">
  <p class="text-sm font-medium">Media used and left</p>
  {#if failed}
    <p role="alert" class="text-sm text-coral">Couldn't load the media limits: {failed}. <Button variant="link" size="xs" onclick={load}>Retry</Button></p>
  {:else if !standing}
    <div class="h-16 animate-pulse rounded-lg bg-raised/60" aria-hidden="true"></div>
    <p class="sr-only" role="status">Loading media limits…</p>
  {:else}
    <ul class="grid gap-1 text-sm">
      {#each standing.kinds as item (item.kind)}
        <li class="flex justify-between gap-3 {item.on ? '' : 'text-muted-foreground'}">
          <span>{KIND_LABELS[item.kind]}</span>
          <span><span class="font-mono">{item.used}</span> used this {item.per} · <span class="font-mono">{item.left}</span> left{item.on ? "" : " (off)"}</span>
        </li>
      {/each}
    </ul>
    <p class="text-sm text-muted-foreground">{standingNote(standing, "here")}</p>
  {/if}
</section>
