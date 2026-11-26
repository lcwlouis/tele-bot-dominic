<script lang="ts">
  import { onMount, untrack } from "svelte";
  import { getJSON, type ServicesView } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import GoogleCard from "./GoogleCard.svelte";
  import ServiceCard from "./ServiceCard.svelte";

  // Settings → Services (E81): what has no other home. `restartNeeded` tells the page to show its Restart banner when
  // a saved change (desktop hosts, the T3 switch) waits for a restart.
  let { revision = $bindable(), restartNeeded = $bindable(false) }: { revision: string; restartNeeded?: boolean } = $props();

  let view = $state<ServicesView | null>(null);
  let failed = $state("");

  function show(loaded: ServicesView) {
    view = loaded;
    restartNeeded = loaded.services.some((service) => service.restart_required);
  }

  async function load() {
    try {
      show(await getJSON<ServicesView>("/api/services"));
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(load);
  $effect(() => {
    const current = revision;
    untrack(() => {
      if (view && current && current !== view.revision) void load();
    });
  });

  function saved(result: ServicesView) {
    show(result);
    revision = result.revision;
  }
</script>

{#if failed && !view}
  <StatusNote tone="error" title="Couldn't load the services" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="secondary" onclick={load}>Retry</Button>
  </StatusNote>
{:else if !view}
  <p class="sr-only" role="status">Loading services…</p>
  <div class="h-64 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  <div class="mb-4 flex items-center gap-1.5">
    <h2 class="text-lg font-semibold">Services</h2>
    <InfoGlyph label="About services">
      Keys and addresses Dom uses outside the model roles. Secret values go to .env and are never shown here.
    </InfoGlyph>
    {#each view.notices as notice (notice.id)}
      <InfoGlyph label={notice.id === "zdr" ? "Zero data retention" : "Note"}>{notice.text}</InfoGlyph>
    {/each}
  </div>
  <div class="grid gap-4 lg:grid-cols-2">
    {#each view.services as service (service.id)}
      <Card.Root class="py-5">
        <Card.Content>
          <ServiceCard {service} revision={view.revision} onsaved={saved} />
        </Card.Content>
      </Card.Root>
    {/each}
    <Card.Root class="py-5">
      <Card.Content><GoogleCard /></Card.Content>
    </Card.Root>
  </div>
{/if}
