<script lang="ts">
  import { onMount } from "svelte";
  import type { PluginRow, PluginsData } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import Notices from "$lib/components/system/Notices.svelte";
  import PluginPanel from "$lib/components/system/PluginPanel.svelte";
  import TrialNote from "$lib/components/system/TrialNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import Confirm from "$lib/components/work/Confirm.svelte";
  import type { Destination, Tab } from "$lib/nav";
  import { WorkPage } from "$lib/work.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  const page = new WorkPage<PluginsData>("/api/system/plugins");
  let retiring = $state<PluginRow | null>(null);
  let deleting = $state<PluginRow | null>(null);

  onMount(() => page.load());

  const tone = (status: string) => (status === "pending" ? "secondary" : status === "approved" ? "default" : "outline");
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#if page.data === null && page.failed}
  <StatusNote tone="error" title="Couldn't load plugins" detail={`Dom's dashboard answered: ${page.failed}.`}>
    <Button variant="outline" onclick={() => page.load()}>Try again</Button>
  </StatusNote>
{:else if page.data === null}
  <div class="h-40 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  {@const data = page.data}
  {#if data.plugins.length === 0}
    <StatusNote title="No plugins are installed" detail="Ask Dom in your DM to write a plugin for something he does often. Review the code here before enabling it." />
  {:else}
    <ul class="grid gap-3 lg:grid-cols-2" aria-label="Plugins">
      {#each data.plugins as row (row.name)}
        <li>
          <Card.Root class="h-full">
            <Card.Header>
              <Card.Title class="flex flex-wrap items-center gap-2 text-base">
                <span class="font-mono">{row.name}</span>
                <Badge variant={tone(row.status)}>{row.status}</Badge>
                {#if !row.available}<Badge variant="destructive">Files unavailable</Badge>{/if}
              </Card.Title>
            </Card.Header>
            <Card.Content class="grid gap-3">
              <Notices network={row.network} storage={row.storage} notices={data.notices} />
              <TrialNote trial={row.test_run} pending={row.available && row.status !== "disabled"} />
              <div class="flex flex-wrap gap-2">
                {#if row.available}
                  <Button href={`/app/system/plugins/${row.name}/${row.hash}`} variant="secondary" size="sm" aria-label={`Review code of ${row.name}`}>Review code</Button>
                {/if}
                {#if row.status !== "disabled"}
                  <Button variant="outline" size="sm" disabled={page.busy} aria-label={`Retire ${row.name}`} onclick={() => (retiring = row)}>Retire</Button>
                {/if}
                <Button variant="destructive" size="sm" disabled={page.busy} aria-label={`Delete ${row.name}`} onclick={() => (deleting = row)}>Delete</Button>
              </div>
            </Card.Content>
          </Card.Root>
        </li>
      {/each}
    </ul>
  {/if}

  {#if data.panels.length}
    <section aria-labelledby="panels" class="mt-8">
      <h2 id="panels" class="kicker mb-3">Plugin panels</h2>
      <div class="grid gap-4 lg:grid-cols-2">
        {#each data.panels as panel (panel.plugin + "/" + panel.id)}
          <PluginPanel plugin={panel.plugin} id={panel.id} title={panel.title} />
        {/each}
      </div>
    </section>
  {/if}
{/if}

<Confirm
  bind:open={() => retiring !== null, (open) => !open && (retiring = null)}
  title={`Retire plugin ${retiring?.name}?`}
  description="Its files stay on disk, and its tools, commands and panels turn off."
  keep="Keep it"
  action="Retire plugin"
  onconfirm={() => retiring && page.write("/api/system/plugins/decide", { name: retiring.name, digest: retiring.hash, decision: "disable" })}
/>
<Confirm
  bind:open={() => deleting !== null, (open) => !open && (deleting = null)}
  title={`Delete plugin ${deleting?.name}?`}
  description="Its files and database state are moved to the deletion archive before removal."
  keep="Keep it"
  action="Delete plugin"
  onconfirm={() => deleting && page.write("/api/system/plugins/delete", { name: deleting.name, expected: deleting.hash })}
/>
