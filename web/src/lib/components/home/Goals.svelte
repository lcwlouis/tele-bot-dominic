<script lang="ts">
  import Check from "@lucide/svelte/icons/check";
  import Pause from "@lucide/svelte/icons/pause";
  import Pencil from "@lucide/svelte/icons/pencil";
  import Play from "@lucide/svelte/icons/play";
  import Plus from "@lucide/svelte/icons/plus";
  import Target from "@lucide/svelte/icons/target";
  import X from "@lucide/svelte/icons/x";
  import { onMount } from "svelte";
  import { getJSON, send, type Goal, type GoalsData, type GoalStatus } from "$lib/api";
  import AppPicker from "$lib/components/kit/AppPicker.svelte";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import * as AlertDialog from "$lib/components/ui/alert-dialog";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { toast } from "$lib/toast.svelte";

  // Goals (R6): what the owner wants to keep an eye on, which Dom checks in on. Active and paused goals show here;
  // Done and Drop ask first, since they close the goal.
  let data = $state<GoalsData | null>(null);
  let failed = $state("");
  let editing = $state<number | "new" | null>(null);
  let form = $state({ text: "", category: "", target_date: "", rhythm: "" });
  let saving = $state(false);
  let closing = $state<{ goal: Goal; status: "done" | "dropped" } | null>(null);

  async function load() {
    try {
      data = await getJSON<GoalsData>("/api/goals");
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(load);

  const open = $derived((data?.goals ?? []).filter((goal) => goal.status === "active" || goal.status === "paused"));
  const closed = $derived((data?.goals ?? []).length - open.length);
  const words = (value: string) => value.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  const categories = $derived([{ value: "", label: "None" }, ...(data?.categories ?? []).map((value) => ({ value, label: words(value) }))]);
  const rhythms = $derived([{ value: "", label: "Dom decides" }, ...(data?.rhythms ?? []).map((value) => ({ value, label: words(value) }))]);
  const due = (date: string) => new Date(`${date}T00:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

  function start(goal: Goal | null) {
    editing = goal?.id ?? "new";
    form = { text: goal?.text ?? "", category: goal?.category ?? "", target_date: goal?.target_date ?? "", rhythm: goal?.check_in_rhythm ?? "" };
  }

  async function write(path: string, body: unknown, done: string) {
    saving = true;
    try {
      const result = await send<GoalsData & { message: string }>(path, body);
      data = { goals: result.goals, categories: result.categories, rhythms: result.rhythms, statuses: result.statuses };
      toast.show("ok", result.message || done);
      return true;
    } catch {
      return false; // the toast says why
    } finally {
      saving = false;
    }
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    const text = form.text.trim();
    if (!text || saving) return;
    const target = form.target_date || null;
    const ok = editing === "new"
      ? await write("/api/goals/create", { text, category: form.category, target_date: target, check_in_rhythm: form.rhythm }, "Goal added.")
      : await write("/api/goals/update", {
          id: editing, text, category: form.category, check_in_rhythm: form.rhythm,
          ...(target ? { target_date: target } : { clear_target_date: true }),
        }, "Goal saved.");
    if (ok) editing = null;
  }

  const setStatus = (goal: Goal, status: GoalStatus) => write("/api/goals/status", { id: goal.id, status }, "Goal updated.");
</script>

{#snippet editor()}
  <form onsubmit={submit} class="grid gap-3 rounded-xl border bg-raised/30 p-3.5" aria-label={editing === "new" ? "New goal" : "Edit goal"}>
    <div class="grid gap-1.5">
      <label for="goal-text" class="text-sm">Goal</label>
      <Input id="goal-text" bind:value={form.text} maxlength={500} required placeholder="Walk 10,000 steps most days" />
    </div>
    <div class="grid gap-3 sm:grid-cols-3">
      <AppPicker label="Category" options={categories} bind:value={form.category} />
      <div class="grid gap-1.5">
        <label for="goal-date" class="text-sm">By</label>
        <Input id="goal-date" type="date" bind:value={form.target_date} class="h-9" />
      </div>
      <AppPicker label="Check in" options={rhythms} bind:value={form.rhythm} />
    </div>
    <div class="flex flex-wrap gap-2">
      <Button type="submit" size="sm" disabled={saving || !form.text.trim()}>{editing === "new" ? "Add goal" : "Save goal"}</Button>
      <Button type="button" size="sm" variant="ghost" onclick={() => (editing = null)} disabled={saving}>Cancel</Button>
    </div>
  </form>
{/snippet}

<Card.Root aria-labelledby="goals-title">
  <Card.Header>
    <div class="flex items-center gap-1.5">
      <Card.Title id="goals-title" class="text-lg">Goals</Card.Title>
      <InfoGlyph label="About goals">What you want to keep an eye on. Dom checks in on each at its rhythm.</InfoGlyph>
    </div>
    <Card.Action>
      <IconButton label="Add a goal" icon={Plus} onclick={() => start(null)} disabled={editing !== null || !data} />
    </Card.Action>
  </Card.Header>
  <Card.Content class="grid gap-3">
    {#if failed && !data}
      <StatusNote tone="error" title="Couldn't load goals" detail={`Dom's dashboard answered: ${failed}.`}>
        <Button variant="secondary" size="sm" onclick={load}>Retry</Button>
      </StatusNote>
    {:else if !data}
      <p role="status" class="text-sm text-muted-foreground">Loading…</p>
    {:else}
      {#if editing === "new"}{@render editor()}{/if}
      {#if open.length}
        <ul class="divide-y border-t">
          {#each open as goal (goal.id)}
            {#if editing === goal.id}
              <li class="py-3">{@render editor()}</li>
            {:else}
              <li class="flex items-start gap-3 py-3">
                <Target class="mt-0.5 size-4 shrink-0 {goal.status === 'paused' ? 'text-muted-foreground' : 'text-signal'}" aria-hidden="true" />
                <div class="min-w-0 flex-1">
                  <p class="break-words {goal.status === 'paused' ? 'text-muted-foreground' : 'font-medium'}">{goal.text}</p>
                  <p class="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                    {#if goal.status === "paused"}<Badge variant="outline">Paused</Badge>{/if}
                    {#if goal.category}<span>{words(goal.category)}</span>{/if}
                    {#if goal.target_date}<span>By {due(goal.target_date)}</span>{/if}
                    {#if goal.check_in_rhythm}<span>Check in {words(goal.check_in_rhythm).toLowerCase()}</span>{/if}
                  </p>
                  {#if goal.progress_note}<p class="mt-1 text-sm text-muted-foreground">{goal.progress_note}</p>{/if}
                </div>
                <div class="flex shrink-0 items-center">
                  <IconButton label={`Edit goal: ${goal.text}`} icon={Pencil} onclick={() => start(goal)} disabled={editing !== null || saving} />
                  {#if goal.status === "active"}
                    <IconButton label={`Pause goal: ${goal.text}`} icon={Pause} onclick={() => setStatus(goal, "paused")} disabled={saving} />
                  {:else}
                    <IconButton label={`Resume goal: ${goal.text}`} icon={Play} onclick={() => setStatus(goal, "active")} disabled={saving} />
                  {/if}
                  <IconButton label={`Mark done: ${goal.text}`} icon={Check} onclick={() => (closing = { goal, status: "done" })} disabled={saving} />
                  <IconButton label={`Drop goal: ${goal.text}`} icon={X} onclick={() => (closing = { goal, status: "dropped" })} disabled={saving} />
                </div>
              </li>
            {/if}
          {/each}
        </ul>
      {:else if editing !== "new"}
        <p class="text-sm text-muted-foreground">No goals yet. Add one and Dom keeps an eye on it with you.</p>
      {/if}
      {#if closed}<p class="text-xs text-muted-foreground">{closed} done or dropped.</p>{/if}
    {/if}
  </Card.Content>
</Card.Root>

<AlertDialog.Root open={closing !== null} onOpenChange={(value) => !value && (closing = null)}>
  <AlertDialog.Content>
    {#if closing}
      <AlertDialog.Header>
        <AlertDialog.Title>{closing.status === "done" ? "Mark this goal done?" : "Drop this goal?"}</AlertDialog.Title>
        <AlertDialog.Description>
          “{closing.goal.text}” leaves the list{closing.status === "done" ? ", and Dom knows you made it." : " and Dom stops checking in on it."}
        </AlertDialog.Description>
      </AlertDialog.Header>
      <AlertDialog.Footer>
        <AlertDialog.Cancel variant="outline">Keep it</AlertDialog.Cancel>
        <AlertDialog.Action
          variant={closing.status === "done" ? "default" : "destructive-solid"}
          onclick={() => {
            const { goal, status } = closing!;
            closing = null;
            setStatus(goal, status);
          }}
        >
          {closing.status === "done" ? "Mark done" : "Drop goal"}
        </AlertDialog.Action>
      </AlertDialog.Footer>
    {/if}
  </AlertDialog.Content>
</AlertDialog.Root>
