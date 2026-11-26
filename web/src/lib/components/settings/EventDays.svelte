<script lang="ts">
  import { onMount } from "svelte";
  import { getJSON, send, type StudioEventDays } from "$lib/api";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { toast } from "$lib/toast.svelte";

  // Days in Dom's own life (a trip, a concert, his birthday) on which he raised his media limits himself. The owner
  // sets the ceiling and the weekly allowance in config.toml ([studio] event_day_max_multiplier, event_days_per_week)
  // and can cancel a day here; the limits go back to normal at once.
  let view = $state<StudioEventDays | null>(null);
  let failed = $state("");
  let cancelling = $state<number | null>(null);

  async function load() {
    try {
      view = await getJSON<StudioEventDays>("/api/studio/event-days");
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }
  onMount(load);

  const label = (day: string) =>
    new Date(day + "T00:00:00Z").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });

  async function cancel(id: number) {
    cancelling = id;
    try {
      view = await send<StudioEventDays>("/api/studio/event-days/cancel", { id });
      toast.show("ok", "Event day cancelled.");
    } catch {
      // send() already showed why
    } finally {
      cancelling = null;
    }
  }
</script>

<Card.Root class="mt-6" role="region" aria-labelledby="event-days-title">
  <Card.Header>
    <Card.Title id="event-days-title" class="text-base">Event days</Card.Title>
    <Card.Description>
      A day in Dom's life (a trip, a concert, his birthday) when he may send more media: up to
      {#if view}<span class="font-mono">{view.ceiling}×</span>{:else}the ceiling{/if} the limits above, at most
      {#if view}<span class="font-mono">{view.per_week}</span>{:else}a few{/if} a week. Each extra item also has to fit the
      month's unused budget.
    </Card.Description>
  </Card.Header>
  <Card.Content>
    {#if failed && !view}
      <p role="alert" class="text-sm text-coral">Couldn't load the event days: {failed}.</p>
      <Button variant="secondary" class="mt-2" onclick={load}>Retry</Button>
    {:else if !view}
      <p role="status" class="text-sm text-muted-foreground">Loading…</p>
    {:else if view.ceiling <= 1}
      <p class="text-sm text-muted-foreground">Event days are off. Set <code>event_day_max_multiplier</code> above 1 to allow them.</p>
    {:else if !view.events.length}
      <p class="text-sm text-muted-foreground">None yet. Dom declares one himself, in your DM, when a day really is an occasion.</p>
    {:else}
      <ul class="grid gap-2">
        {#each view.events as event (event.id)}
          <li class="flex flex-wrap items-center justify-between gap-3 rounded-xl border bg-surface p-3">
            <span class={["text-sm", event.cancelled && "text-muted-foreground line-through"]}>
              <span class="font-medium">{label(event.day)}</span> · {event.title}
              <span class="font-mono text-muted-foreground">({Math.min(event.multiplier, view.ceiling)}×)</span>
              {#if event.active}<span class="ml-1 text-sm">on now</span>{/if}
            </span>
            {#if !event.cancelled}
              <Button
                variant="secondary"
                size="sm"
                disabled={cancelling !== null}
                onclick={() => cancel(event.id)}
                aria-label={`Cancel the event day ${label(event.day)}: ${event.title}`}
              >
                Cancel
              </Button>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </Card.Content>
</Card.Root>
