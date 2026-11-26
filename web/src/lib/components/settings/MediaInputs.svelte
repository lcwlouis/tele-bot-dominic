<script lang="ts">
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import { onMount } from "svelte";
  import { getJSON, send, type MediaInputs } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { toast } from "$lib/toast.svelte";

  // What each chat model takes as input (E73): photos and voice go to it natively when it accepts them, and as a
  // description otherwise. "Probe again" re-runs the one-off image test on models without an `input` setting.
  let data = $state<MediaInputs | null>(null);
  let failed = $state("");
  let probing = $state(false);

  async function load() {
    try {
      data = await getJSON<MediaInputs>("/api/media/inputs");
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(load);

  async function probe() {
    probing = true;
    try {
      data = await send<MediaInputs>("/api/media/inputs/probe", {});
      toast.show("ok", "Probed the chat models again.");
    } catch {
      // the toast says why
    } finally {
      probing = false;
    }
  }

  const SOURCE: Record<string, string> = {
    config: "Set in config.toml", list: "From the provider's model list", probe: "Found by a test image", unknown: "Not known yet",
  };
  const INPUT: Record<string, string> = { text: "Text", image: "Photos", audio: "Voice", video: "Video", file: "Files" };
  const word = (input: string) => INPUT[input] ?? input;
</script>

<Card.Root role="region" aria-labelledby="media-inputs-title">
  <Card.Header>
    <div class="flex items-center gap-1.5">
      <Card.Title id="media-inputs-title" class="text-lg">What the chat models take</Card.Title>
      <InfoGlyph label="About media inputs">
        A photo or voice note goes straight to a chat model that takes it. For the rest, the photos and voice model
        describes it first.
      </InfoGlyph>
    </div>
    <Card.Action>
      <Button variant="ghost" size="sm" onclick={probe} disabled={probing}>
        {#if probing}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" />{:else}<RefreshCw aria-hidden="true" />{/if}
        Probe again
      </Button>
    </Card.Action>
  </Card.Header>
  <Card.Content>
    {#if failed && !data}
      <StatusNote tone="error" title="Couldn't load the inputs" detail={`Dom's dashboard answered: ${failed}.`}>
        <Button variant="secondary" onclick={load}>Retry</Button>
      </StatusNote>
    {:else if !data}
      <p role="status" class="text-sm text-muted-foreground">Loading…</p>
    {:else}
      <p class="mb-3 text-sm">
        The chat route takes: {data.route_accepts.length ? data.route_accepts.map(word).join(", ") : "text only"}.
      </p>
      <ul class="divide-y border-t">
        {#each data.providers as provider, index (index)}
          <li class="flex flex-wrap items-center gap-x-3 gap-y-1 py-2.5">
            <span class="min-w-0 flex-1 basis-48 font-mono text-sm break-all">{provider.model}</span>
            <span class="flex flex-wrap gap-1">
              {#each provider.accepts as input (input)}<Badge variant="secondary">{word(input)}</Badge>{:else}<Badge variant="outline">Unknown</Badge>{/each}
            </span>
            <span class="basis-full text-xs text-muted-foreground sm:basis-auto">{SOURCE[provider.source] ?? provider.source}</span>
          </li>
        {/each}
      </ul>
    {/if}
  </Card.Content>
</Card.Root>
