<script lang="ts">
  import ArrowUpRight from "@lucide/svelte/icons/arrow-up-right";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import type { Destination, Tab } from "$lib/nav";

  // Destinations not ported yet: the baseline shows where each page lives and links to the current page.
  let { destination, tab }: { destination: Destination; tab?: Tab } = $props();
  const page = $derived(tab ?? destination);
  const legacy = $derived(tab?.legacy ?? destination.legacy ?? "/");
</script>

<PageHeader kicker={tab ? destination.label : "Destination"} title={page.label} subtitle={page.blurb} />

<PageTabs {destination} {tab} />

<Card.Root class="orbit-grid max-w-2xl">
  <Card.Header>
    <p class="kicker">Not ported yet</p>
    <Card.Title class="text-lg">{page.label} still lives in the current dashboard</Card.Title>
    <Card.Description>
      {page.label} moves to the new dashboard in a later update. Until then the current page has everything.
    </Card.Description>
  </Card.Header>
  <Card.Footer>
    <Button href={legacy} variant="secondary">
      Open {page.label} in the current dashboard <ArrowUpRight data-icon="inline-end" aria-hidden="true" />
    </Button>
  </Card.Footer>
</Card.Root>
