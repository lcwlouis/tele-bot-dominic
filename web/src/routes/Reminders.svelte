<script lang="ts">
  import Bell from "@lucide/svelte/icons/bell";
  import { onMount } from "svelte";
  import type { RemindersData, Reminder } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import GoneNote from "$lib/components/kit/GoneNote.svelte";
  import { LINKED, linkedId, reveal } from "$lib/deep-link";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Checkbox } from "$lib/components/ui/checkbox";
  import Confirm from "$lib/components/work/Confirm.svelte";
  import type { Destination, Tab } from "$lib/nav";
  import { WorkPage } from "$lib/work.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  const page = new WorkPage<RemindersData>("/api/work/reminders");
  // A link may name one reminder (?id=<id>, from a Telegram notice or a search hit): the page asks for it by name,
  // so an older one than the list holds still comes, then selects it in place.
  const linked = linkedId("id");
  let gone = $state(false);
  let cancelling = $state<Reminder | null>(null);
  let deleting = $state<Reminder | null>(null);
  // Bulk: the selected ids, and which action waits on its one confirm.
  let selected = $state<number[]>([]);
  let bulk = $state<"cancel" | "delete" | null>(null);

  onMount(async () => {
    await page.load(linked === null ? "" : `?id=${linked}`);
    if (linked === null || page.data === null) return;
    if (page.data.reminders.some((row) => row.id === linked)) reveal(`reminder-row-${linked}`);
    else gone = true;
  });

  const tone = (row: Reminder) => (row.status === "active" ? (row.undeliverable ? "destructive" : "secondary") : "outline");
  const label = (row: Reminder) => (row.status !== "active" ? row.status : row.undeliverable ? "undeliverable" : "active");
  const rows = $derived(page.data?.reminders ?? []);
  // A reminder that left the list (deleted elsewhere) drops out of the selection.
  const chosen = $derived(rows.filter((row) => selected.includes(row.id)));
  const active = $derived(chosen.filter((row) => row.status === "active"));
  const all = $derived(rows.length > 0 && chosen.length === rows.length);
  const plural = (count: number) => `${count} reminder${count === 1 ? "" : "s"}`;

  function toggle(id: number, on: boolean) {
    selected = on ? [...selected, id] : selected.filter((other) => other !== id);
  }

  async function runBulk(action: "cancel" | "delete") {
    const targets = action === "cancel" ? active : chosen;
    const items = targets.map((row) => ({ id: row.id, expected: row.created_at }));
    if (items.length && (await page.write("/api/work/reminders/bulk", { action, items }))) selected = [];
    else selected = selected.filter((id) => rows.some((row) => row.id === id)); // keep what is still there to retry
  }
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#if gone}<GoneNote text="That reminder is gone. It may have been deleted." ondismiss={() => (gone = false)} />{/if}

{#if page.data === null && page.failed}
  <StatusNote tone="error" title="Couldn't load reminders" detail={`Dom's dashboard answered: ${page.failed}.`}>
    <Button variant="outline" onclick={() => page.load()}>Try again</Button>
  </StatusNote>
{:else if page.data === null}
  <div class="h-40 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else if rows.length === 0}
  <StatusNote title="No reminders yet" detail="Ask Dom to remind you of something and it shows up here." />
{:else}
  <!-- Sticky on a phone, so the bulk actions stay in reach while picking from a long list. -->
  <div
    role="toolbar"
    aria-label="Selected reminders"
    class="sticky top-0 z-10 mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border bg-surface/95 px-4 py-2.5 backdrop-blur"
  >
    <label class="flex items-center gap-2 text-sm font-medium">
      <Checkbox
        checked={all}
        indeterminate={chosen.length > 0 && !all}
        onCheckedChange={(on) => (selected = on ? rows.map((row) => row.id) : [])}
      />
      Select all
    </label>
    <span class="text-sm text-muted-foreground" role="status">{chosen.length ? `${chosen.length} selected` : "None selected"}</span>
    <div class="ml-auto flex flex-wrap gap-2">
      <Button variant="outline" size="sm" disabled={page.busy || active.length === 0} aria-label={`Cancel ${plural(active.length)} selected`} onclick={() => (bulk = "cancel")}>
        Cancel{active.length ? ` ${active.length}` : ""}
      </Button>
      <Button variant="destructive" size="sm" disabled={page.busy || chosen.length === 0} aria-label={`Delete ${plural(chosen.length)} selected`} onclick={() => (bulk = "delete")}>
        Delete{chosen.length ? ` ${chosen.length}` : ""}
      </Button>
    </div>
  </div>
  <ul class="grid gap-3" aria-label="Reminders">
    {#each rows as row (row.id)}
      <li id={`reminder-row-${row.id}`} tabindex="-1" class={row.id === linked ? LINKED : "rounded-xl outline-none"}>
        <Card.Root class={selected.includes(row.id) ? "border-signal/45" : ""}>
          <Card.Content class="flex flex-wrap items-center gap-x-4 gap-y-3">
            <Checkbox
              id={`reminder-${row.id}`}
              aria-label={`Select reminder ${row.id}`}
              checked={selected.includes(row.id)}
              onCheckedChange={(on) => toggle(row.id, on === true)}
            />
            <span class="hidden size-9 shrink-0 place-items-center rounded-lg border bg-raised text-signal sm:grid" aria-hidden="true"><Bell class="size-4" /></span>
            <div class="min-w-0 flex-1">
              <p class="font-medium break-words">{row.text}</p>
              <p class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted-foreground">
                <span class="font-mono">#{row.id} · {row.due}</span>
                <span>{row.importance}{row.repeat ? `, ${row.repeat}` : ""}</span>
                <Badge variant={tone(row)}>{label(row)}</Badge>
              </p>
            </div>
            <!-- On a phone the buttons take their own line, so the reminder's text keeps the width. -->
            <div class="flex gap-2 max-sm:basis-full max-sm:justify-end">
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
<Confirm
  bind:open={() => bulk !== null, (open) => !open && (bulk = null)}
  title={bulk === "cancel" ? `Cancel ${plural(active.length)}?` : `Delete ${plural(chosen.length)}?`}
  description={bulk === "cancel"
    ? `Dom won't remind you of these. They stay in the list as cancelled.${active.length < chosen.length ? " Selected reminders that are already done or cancelled are left as they are." : ""}`
    : "A copy of each is saved to the deletion archive."}
  keep="Keep them"
  action={bulk === "cancel" ? "Cancel reminders" : "Delete reminders"}
  onconfirm={() => bulk && runBulk(bulk)}
/>
