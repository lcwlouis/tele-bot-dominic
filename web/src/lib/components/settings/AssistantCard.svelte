<script lang="ts">
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { onMount, untrack } from "svelte";
  import { getJSON, send, type AssistantView } from "$lib/api";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { toast } from "$lib/toast.svelte";

  // Assistant mode's schedule ([assistant] morning and evening) and what became of each message's last day. The switch
  // itself is the row above, in "What Dom may do". A save takes the revision that came with the times shown and hands
  // the page the one it returns; the scheduler reads the times on every tick, so a save applies at once.
  let { revision = $bindable(), enabled }: { revision: string; enabled: boolean } = $props();

  const NAMES = { morning: "Morning plan", evening: "Evening wrap-up" } as const;
  let view = $state<AssistantView | null>(null);
  let failed = $state("");
  let draft = $state<Record<"morning" | "evening", string>>({ morning: "", evening: "" });
  let saving = $state(false);
  const changed = $derived(view !== null && view.messages.some((message) => message.time !== draft[message.id]));

  async function load() {
    try {
      const loaded = await getJSON<AssistantView>("/api/assistant");
      if (view === null || !changed) {
        draft = Object.fromEntries(loaded.messages.map((message) => [message.id, message.time])) as typeof draft;
      }
      view = loaded;
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }
  onMount(load);
  // Another save on the page, or the switch above, moved the file: read the schedule and the outcomes again.
  $effect(() => {
    void revision;
    void enabled;
    untrack(() => view !== null && load());
  });

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!view || !changed || saving) return;
    saving = true;
    try {
      const result = await send<AssistantView>("/api/assistant/schedule", {
        revision: view.revision, morning: draft.morning, evening: draft.evening,
      });
      view = result;
      revision = result.revision;
      toast.show("ok", "Assistant schedule saved.");
    } catch {
      // send() already showed why
    } finally {
      saving = false;
    }
  }

  const when = (iso: string) => new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
</script>

<Card.Root class="mt-6" role="region" aria-labelledby="assistant-schedule-title">
  <Card.Header>
    <Card.Title id="assistant-schedule-title" class="text-base">Assistant schedule</Card.Title>
    <Card.Description>
      The time of each message in your timezone{view ? ` (${view.timezone})` : ""}. Leave one empty to turn it off.
      A message goes out once a day, and late only within three hours of its time. It waits while Dom is asleep, and
      says nothing on a day with nothing to say.
    </Card.Description>
  </Card.Header>
  <Card.Content>
    {#if failed}
      <StatusNote title="Could not load the schedule" detail={failed} />
    {:else if view}
      <form class="grid gap-4" onsubmit={save}>
        {#each view.messages as message (message.id)}
          <div class="grid gap-1.5 sm:grid-cols-[12rem_auto_1fr] sm:items-center sm:gap-4">
            <label for={`assistant-${message.id}`} class="text-sm font-medium">{NAMES[message.id]}</label>
            <Input id={`assistant-${message.id}`} class="w-32" type="time" bind:value={draft[message.id]} />
            <p class="text-sm text-muted-foreground" aria-live="polite">
              {#if !enabled}
                Assistant mode is off.
              {:else if message.last}
                Last, {message.last.day}: {message.last.label.charAt(0).toLowerCase() + message.last.label.slice(1)}
                ({when(message.last.at)})
              {:else if message.time}
                Not sent yet.
              {:else}
                Off.
              {/if}
            </p>
          </div>
        {/each}
        <div>
          <Button type="submit" disabled={!changed || saving}>
            {#if saving}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…{:else}Save schedule{/if}
          </Button>
        </div>
      </form>
    {:else}
      <p class="text-sm text-muted-foreground">Loading…</p>
    {/if}
  </Card.Content>
</Card.Root>
