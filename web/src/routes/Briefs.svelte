<script lang="ts">
  import Copy from "@lucide/svelte/icons/copy";
  import { onMount } from "svelte";
  import type { BriefsData } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import type { Destination, Tab } from "$lib/nav";
  import { toast } from "$lib/toast.svelte";
  import { WorkPage } from "$lib/work.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  const page = new WorkPage<BriefsData>("/api/work/briefs");

  onMount(() => page.load());

  async function copy(markdown: string) {
    try {
      await navigator.clipboard.writeText(markdown);
      toast.show("ok", "Brief copied.");
    } catch {
      toast.show("error", "Couldn't copy. Select the text and copy it instead.");
    }
  }
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#if page.data === null && page.failed}
  <StatusNote tone="error" title="Couldn't load the briefs" detail={`Dom's dashboard answered: ${page.failed}.`}>
    <Button variant="outline" onclick={() => page.load()}>Try again</Button>
  </StatusNote>
{:else if page.data === null}
  <div class="h-40 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else if page.data.briefs.length === 0}
  <StatusNote
    title="No core-change briefs yet"
    detail="When Dom thinks his own code should change, he writes a brief here for a coding agent."
  />
{:else}
  <ul class="grid gap-4" aria-label="Briefs">
    {#each page.data.briefs as brief (brief.filename)}
      <li>
        <Card.Root>
          <Card.Header>
            <Card.Title class="text-lg">{brief.title}</Card.Title>
            <Card.Description class="font-mono">{brief.filename}</Card.Description>
          </Card.Header>
          <Card.Content class="grid gap-3 text-[0.95rem]">
            {#each brief.blocks as block, index (index)}
              {#if block.kind === "heading"}
                <h2 class="mt-2 text-base font-semibold">{block.lines[0]}</h2>
              {:else if block.kind === "code"}
                <pre class="overflow-x-auto rounded-lg border bg-canvas/50 p-3 font-mono text-sm">{block.lines.join("\n")}</pre>
              {:else if block.kind === "ul"}
                <ul class="list-disc space-y-1 pl-5">{#each block.lines as line}<li>{line}</li>{/each}</ul>
              {:else if block.kind === "ol"}
                <ol class="list-decimal space-y-1 pl-5">{#each block.lines as line}<li>{line}</li>{/each}</ol>
              {:else}
                <p class="whitespace-pre-line break-words">{block.lines.join("\n")}</p>
              {/if}
            {/each}
          </Card.Content>
          <Card.Footer>
            <Button variant="outline" size="sm" onclick={() => copy(brief.markdown)}>
              <Copy aria-hidden="true" /> Copy<span class="sr-only"> {brief.title}</span>
            </Button>
          </Card.Footer>
        </Card.Root>
      </li>
    {/each}
  </ul>
{/if}
