<script lang="ts">
  import Lightbulb from "@lucide/svelte/icons/lightbulb";
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { send, type ForYou, type NoticingsData } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { toast } from "$lib/toast.svelte";

  // "For you" (R7): what Dom noticed in his nightly reflection, as a friend would say it, not a task inbox. "Let's do
  // it" hands it to Dom to act on; "Not now" lets it go. Either way it leaves this list.
  let { data, onchange }: { data: ForYou; onchange?: () => void } = $props();

  let gone = $state<number[]>([]);
  let deciding = $state<number | null>(null);
  const items = $derived(data.items.filter((item) => !gone.includes(item.id)));
  const KIND: Record<string, string> = { deadline: "Coming up", help: "Could help", followup: "Follow up", idea: "Idea" };

  async function decide(id: number, decision: "accept" | "dismiss") {
    deciding = id;
    try {
      const result = await send<NoticingsData & { ok: boolean; message: string }>("/api/noticings/decide", { id, decision });
      gone = [...gone, id];
      toast.show(result.ok ? "ok" : "error", result.message || (decision === "accept" ? "Dom's on it." : "Let go."));
      onchange?.();
    } catch {
      // the toast says why; the noticing stays
    } finally {
      deciding = null;
    }
  }
</script>

<Card.Root aria-labelledby="for-you-title">
  <Card.Header>
    <div class="flex items-center gap-1.5">
      <Card.Title id="for-you-title" class="text-lg">For you</Card.Title>
      <InfoGlyph label="About For you">Things Dom noticed in his nightly reflection. He may bring one up in your DM.</InfoGlyph>
    </div>
    <Card.Action><Lightbulb class="size-4 text-signal" aria-hidden="true" /></Card.Action>
  </Card.Header>
  <Card.Content>
    {#if items.length}
      <ul class="divide-y border-t">
        {#each items as item (item.id)}
          <li class="grid gap-2 py-3">
            <div class="flex flex-wrap items-center gap-2">
              <Badge variant="secondary">{KIND[item.kind] ?? item.kind}</Badge>
              <span class="min-w-0 font-medium">{item.title}</span>
            </div>
            {#if item.body}<p class="text-sm text-muted-foreground">{item.body}</p>{/if}
            {#if item.source_label}<p class="text-xs text-muted-foreground">From {item.source_label}</p>{/if}
            <div class="flex flex-wrap gap-2">
              <Button size="sm" onclick={() => decide(item.id, "accept")} disabled={deciding !== null}>
                {#if deciding === item.id}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" />{/if}
                Let's do it <span class="sr-only">: {item.title}</span>
              </Button>
              <Button size="sm" variant="ghost" onclick={() => decide(item.id, "dismiss")} disabled={deciding !== null}>
                Not now <span class="sr-only">: {item.title}</span>
              </Button>
            </div>
          </li>
        {/each}
      </ul>
      {#if data.count > data.items.length}
        <p class="mt-2 text-sm text-muted-foreground">{data.count - data.items.length} more after these.</p>
      {/if}
    {:else}
      <p class="text-sm text-muted-foreground">Nothing new. Dom shares what he notices after his nightly reflection.</p>
    {/if}
  </Card.Content>
</Card.Root>
