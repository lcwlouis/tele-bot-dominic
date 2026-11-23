<script lang="ts">
  import Bell from "@lucide/svelte/icons/bell";
  import { onMount } from "svelte";
  import type { RemindersData, Reminder } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import Confirm from "$lib/components/work/Confirm.svelte";
  import type { Destination, Tab } from "$lib/nav";
  import { WorkPage } from "$lib/work.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  const page = new WorkPage<RemindersData>("/api/work/reminders");
  let cancelling = $state<Reminder | null>(null);
  let deleting = $state<Reminder | null>(null);

  onMount(() => page.load());

  const tone = (row: Reminder) => (row.status === "active" ? (row.undeliverable ? "destructive" : "secondary") : "outline");
  const label = (row: Reminder) => (row.status !== "active" ? row.status : row.undeliverable ? "undeliverable" : "active");
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#if page.data === null && page.failed}
  <StatusNote tone="error" title="Couldn't load reminders" detail={`Dom's dashboard answered: ${page.failed}.`}>
    <Button variant="outline" onclick={() => page.load()}>Try again</Button>
  </StatusNote>
{:else if page.data === null}
  <div class="h-40 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else if page.data.reminders.length === 0}
  <StatusNote title="No reminders yet" detail="Ask Dom to remind you of something and it shows up here." />
{:else}
  <ul class="grid gap-3" aria-label="Reminders">
    {#each page.data.reminders as row (row.id)}
      <li>
        <Card.Root>
          <Card.Content class="flex flex-wrap items-center gap-x-4 gap-y-3">
            <span class="grid size-9 shrink-0 place-items-center rounded-lg border bg-raised text-signal" aria-hidden="true"><Bell class="size-4" /></span>
            <div class="min-w-0 flex-1">
              <p class="font-medium break-words">{row.text}</p>
              <p class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted-foreground">
                <span class="font-mono">#{row.id} · {row.due}</span>
                <span>{row.importance}{row.repeat ? `, ${row.repeat}` : ""}</span>
                <Badge variant={tone(row)}>{label(row)}</Badge>
              </p>
            </div>
            <div class="flex gap-2">
              {#if row.status === "active"}
                <Button variant="outline" size="sm" disabled={page.busy} aria-label={`Cancel reminder ${row.id}`} onclick={() => (cancelling = row)}>Cancel</Button>
              {/if}
              <Button variant="destructive" size="sm" disabled={page.busy} aria-label={`Delete reminder ${row.id}`} onclick={() => (deleting = row)}>Delete</Button>
            </div>
          </Card.Content>
        </Card.Root>
      </li>
    {/each}
  </ul>
{/if}

<Confirm
  bind:open={() => cancelling !== null, (open) => !open && (cancelling = null)}
  title={`Cancel reminder #${cancelling?.id}?`}
  description="Dom won't remind you of this. It stays in the list as cancelled."
  keep="Keep it"
  action="Cancel reminder"
  onconfirm={() => cancelling && page.write("/api/work/reminders/cancel", { id: cancelling.id })}
/>
<Confirm
  bind:open={() => deleting !== null, (open) => !open && (deleting = null)}
  title={`Delete reminder #${deleting?.id}?`}
  description="A copy is saved to the deletion archive."
  keep="Keep it"
  action="Delete reminder"
  onconfirm={() => deleting && page.write("/api/work/reminders/delete", { id: deleting.id, expected: deleting.created_at })}
/>
