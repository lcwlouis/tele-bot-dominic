<script lang="ts">
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { onMount } from "svelte";
  import { getJSON, send, type SpendArea, type SpendView } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { Progress } from "$lib/components/ui/progress";
  import { Switch } from "$lib/components/ui/switch";
  import type { Destination, Tab } from "$lib/nav";
  import { toast } from "$lib/toast.svelte";
  import { cn } from "$lib/utils";

  // Spend limits in the app (E86): today against the daily cap, the month against the monthly one, each area with
  // its own limit, 30 days by area, and the caps themselves. Every figure comes from the recorded calls.
  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  const MAX = 100_000; // dom/web/spend_settings.py MAX_CAP_USD
  let view = $state<SpendView | null>(null);
  let failed = $state("");
  let saving = $state(false);
  // The editor's text, so a half-typed number stays as typed. Empty means off (or no area limit).
  let draft = $state<{ daily: string; monthly: string; ownerDm: boolean; areas: Record<SpendArea, string> } | null>(null);

  const asText = (cap: number | null) => (cap ? String(cap) : "");

  function show(loaded: SpendView) {
    view = loaded;
    draft = {
      daily: asText(loaded.today.cap),
      monthly: asText(loaded.month.cap),
      ownerDm: loaded.cap_owner_dm,
      areas: Object.fromEntries(loaded.areas.map((area) => [area.area, asText(area.cap)])) as Record<SpendArea, string>,
    };
  }

  async function load() {
    try {
      show(await getJSON<SpendView>("/api/spend"));
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(load);

  const money = (amount: number) => `$${amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  /** A typed amount: null when empty, NaN when it isn't a valid one. */
  function amount(text: string): number | null {
    const trimmed = text.trim();
    if (trimmed === "") return null;
    const value = Number(trimmed);
    return Number.isFinite(value) && value >= 0 && value <= MAX ? value : NaN;
  }

  const problems = $derived.by(() => {
    const out: Record<string, string> = {};
    if (!draft) return out;
    const range = `Enter an amount from 0 to ${MAX.toLocaleString("en-US")}, or leave it empty.`;
    const daily = amount(draft.daily);
    const monthly = amount(draft.monthly);
    if (Number.isNaN(daily)) out.daily = range;
    if (Number.isNaN(monthly)) out.monthly = range;
    else if (daily && monthly && daily > monthly) out.monthly = "The monthly cap must be at least the daily cap.";
    for (const [area, text] of Object.entries(draft.areas)) if (Number.isNaN(amount(text))) out[area] = range;
    return out;
  });
  const valid = $derived(Object.keys(problems).length === 0);
  const body = $derived.by(() => {
    if (!view || !draft || !valid) return null;
    return {
      revision: view.revision,
      daily_spend_cap_usd: amount(draft.daily) ?? 0,
      monthly_spend_cap_usd: amount(draft.monthly) ?? 0,
      cap_owner_dm: draft.ownerDm,
      area_caps: Object.fromEntries(Object.entries(draft.areas).map(([area, text]) => [area, amount(text) || null])),
    };
  });
  const changed = $derived.by(() => {
    if (!view || !body) return false;
    const saved = {
      daily: view.today.cap ?? 0, monthly: view.month.cap ?? 0, owner: view.cap_owner_dm,
      areas: Object.fromEntries(view.areas.map((area) => [area.area, area.cap || null])),
    };
    return JSON.stringify(saved) !== JSON.stringify({
      daily: body.daily_spend_cap_usd, monthly: body.monthly_spend_cap_usd, owner: body.cap_owner_dm, areas: body.area_caps,
    });
  });

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!body || !changed || saving) return;
    saving = true;
    try {
      show(await send<SpendView>("/api/spend/caps", body));
      toast.show("ok", "Spend limits saved. They apply at once.");
    } catch {
      // the toast says why; what was typed stays
    } finally {
      saving = false;
    }
  }

  // Each area keeps its colour wherever it shows (chart tokens, checked for colour-blind separation).
  const FILL: Record<SpendArea, string> = {
    chat: "fill-chart-1", media: "fill-chart-2", search: "fill-chart-3", desktop: "fill-chart-4", memory: "fill-chart-5",
  };
  const BAR = 10; // each day's slot in the chart's 300-wide box
  const HEIGHT = 100;

  function chart(data: SpendView) {
    const top = Math.max(...data.daily.map((day) => day.spent), data.today.cap ?? 0, 0.01);
    const scale = (value: number) => (value / top) * (HEIGHT - 4);
    return {
      top,
      cap: data.today.cap ? HEIGHT - scale(data.today.cap) : null,
      days: data.daily.map((day, index) => {
        let y = HEIGHT;
        const parts = data.areas
          .map((area) => ({ area: area.area, label: area.label, spent: day.areas[area.area] ?? 0 }))
          .filter((part) => part.spent > 0)
          .map((part) => {
            const height = Math.max(scale(part.spent), 0.8);
            y -= height;
            return { ...part, y, height };
          });
        return { ...day, x: index * BAR, parts };
      }),
    };
  }

  const dayLabel = (date: string) => new Date(`${date}T00:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  const resets = (date: string) => new Date(`${date}T00:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "long", timeZone: "UTC" });
  let hovered = $state<string | null>(null);
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#snippet meter(id: string, title: string, spent: number, cap: number | null, left: number | null, off: string, note: string)}
  <Card.Root aria-labelledby={id}>
    <Card.Header>
      <Card.Title id={id} class="text-lg">{title}</Card.Title>
    </Card.Header>
    <Card.Content class="grid gap-2">
      <p class="font-mono text-3xl font-semibold tracking-tight">{money(spent)}</p>
      {#if cap}
        <Progress value={Math.min(spent, cap)} max={cap} aria-label={`${title}: ${money(spent)} of ${money(cap)}`} />
        <p class="text-sm text-muted-foreground">of {money(cap)} · <span class="font-mono">{money(left ?? 0)}</span> left</p>
        {#if (left ?? 0) <= 0}<p class="text-sm font-medium text-coral">Cap reached: friends' chats and background work wait.</p>{/if}
      {:else}
        <p class="text-sm text-muted-foreground">{off}</p>
      {/if}
      {#if note}<p class="text-sm text-muted-foreground">{note}</p>{/if}
    </Card.Content>
  </Card.Root>
{/snippet}

{#if view === null && failed}
  <StatusNote tone="error" title="Couldn't load spend" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="outline" onclick={load}>Try again</Button>
  </StatusNote>
{:else if view === null || draft === null}
  <p class="sr-only" role="status">Loading spend…</p>
  <div class="h-56 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  {@const data = view}
  {@const plot = chart(data)}
  <div class="grid gap-4 md:grid-cols-2">
    {@render meter("spend-today", "Today", data.today.spent, data.today.cap, data.today.left, "No daily cap.", "")}
    {@render meter(
      "spend-month", "This month", data.month.spent, data.month.cap, data.month.left, "No monthly cap.",
      `Resets on ${resets(data.month_ends)}.${data.one_off_room !== null ? ` Event days may still draw ${money(data.one_off_room)}.` : ""}`,
    )}
  </div>

  <Card.Root class="mt-4" aria-labelledby="spend-areas-title">
    <Card.Header>
      <Card.Title id="spend-areas-title" class="text-lg">This month by area</Card.Title>
    </Card.Header>
    <Card.Content>
      <ul class="divide-y border-t">
        {#each data.areas as area (area.area)}
          <li class="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 py-3">
            <svg viewBox="0 0 10 10" class="size-2.5" aria-hidden="true"><rect width="10" height="10" rx="2" class={FILL[area.area]} /></svg>
            <span class="flex min-w-0 items-center gap-1">
              <span class="truncate">{area.label}</span>
              <InfoGlyph label={`About ${area.label}`}>{area.help}</InfoGlyph>
            </span>
            <span class="text-right text-sm">
              <span class="font-mono">{money(area.spent)}</span>
              {#if area.cap}<span class="text-muted-foreground"> of {money(area.cap)}</span>{/if}
            </span>
            {#if area.cap}
              <div class="col-start-2 col-end-4">
                <Progress value={Math.min(area.spent, area.cap)} max={area.cap} aria-label={`${area.label}: ${money(area.spent)} of ${money(area.cap)}`} />
                {#if (area.left ?? 0) <= 0}<p class="mt-1 text-sm text-coral">At its limit: this work waits until the 1st.</p>{/if}
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    </Card.Content>
  </Card.Root>

  <Card.Root class="mt-4" aria-labelledby="spend-chart-title">
    <Card.Header>
      <Card.Title id="spend-chart-title" class="text-lg">Last 30 days</Card.Title>
      <Card.Description>Daily spend by area{data.today.cap ? ", with the daily cap as a dashed line" : ""}.</Card.Description>
    </Card.Header>
    <Card.Content>
      <figure>
        <div class="flex justify-between text-xs text-muted-foreground" aria-hidden="true">
          <span class="font-mono">{money(plot.top)}</span>
          <span>{hovered ? `${dayLabel(hovered)}: ${money(data.daily.find((day) => day.date === hovered)?.spent ?? 0)}` : ""}</span>
        </div>
        <svg
          viewBox={`0 0 ${BAR * data.daily.length} ${HEIGHT}`}
          preserveAspectRatio="none"
          class="mt-1 h-44 w-full"
          role="img"
          aria-label={`Daily spend by area over the last 30 days, from ${money(0)} to ${money(plot.top)}. The table below has every figure.`}
        >
          <line x1="0" x2={BAR * data.daily.length} y1={HEIGHT - 0.25} y2={HEIGHT - 0.25} class="stroke-line" stroke-width="0.5" vector-effect="non-scaling-stroke" />
          {#each plot.days as day (day.date)}
            <g role="presentation" onpointerenter={() => (hovered = day.date)} onpointerleave={() => (hovered = null)}>
              <title>{dayLabel(day.date)}: {money(day.spent)}{day.parts.map((part) => `\n${part.label}: ${money(part.spent)}`).join("")}</title>
              <!-- A taller hit target than the bar itself. -->
              <rect x={day.x} y="0" width={BAR} height={HEIGHT} class="fill-transparent" />
              {#each day.parts as part (part.area)}
                <rect x={day.x + 1} y={part.y} width={BAR - 2} height={part.height} class={cn(FILL[part.area], "stroke-surface")} stroke-width="1.5" vector-effect="non-scaling-stroke" />
              {/each}
            </g>
          {/each}
          {#if plot.cap !== null}
            <line x1="0" x2={BAR * data.daily.length} y1={plot.cap} y2={plot.cap} class="stroke-muted-foreground" stroke-width="1" stroke-dasharray="4 3" vector-effect="non-scaling-stroke" />
          {/if}
        </svg>
        <div class="mt-1 flex justify-between text-xs text-muted-foreground" aria-hidden="true">
          <span>{dayLabel(data.daily[0]?.date ?? data.month_ends)}</span>
          <span>{dayLabel(data.daily.at(-1)?.date ?? data.month_ends)}</span>
        </div>
        <figcaption>
          <ul class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-label="Chart key">
            {#each data.areas as area (area.area)}
              <li class="flex items-center gap-1.5">
                <svg viewBox="0 0 10 10" class="size-2.5" aria-hidden="true"><rect width="10" height="10" rx="2" class={FILL[area.area]} /></svg>{area.label}
              </li>
            {/each}
          </ul>
        </figcaption>
      </figure>
      <details class="mt-4">
        <summary class="cursor-pointer text-sm font-medium">Show as a table</summary>
        <div class="mt-3 overflow-x-auto">
          <table class="w-full min-w-[36rem] text-sm">
            <caption class="sr-only">Daily spend by area, last 30 days</caption>
            <thead>
              <tr class="text-left text-muted-foreground">
                <th scope="col" class="py-1.5 pr-3 font-medium">Day</th>
                {#each data.areas as area (area.area)}<th scope="col" class="px-2 py-1.5 text-right font-medium">{area.label}</th>{/each}
                <th scope="col" class="py-1.5 pl-3 text-right font-medium">Total</th>
              </tr>
            </thead>
            <tbody>
              {#each [...data.daily].reverse() as day (day.date)}
                <tr class="border-t">
                  <th scope="row" class="py-1.5 pr-3 text-left font-normal">{dayLabel(day.date)}</th>
                  {#each data.areas as area (area.area)}<td class="px-2 py-1.5 text-right font-mono">{money(day.areas[area.area] ?? 0)}</td>{/each}
                  <td class="py-1.5 pl-3 text-right font-mono">{money(day.spent)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </details>
    </Card.Content>
  </Card.Root>

  <Card.Root class="mt-4" role="region" aria-labelledby="spend-caps-title">
    <Card.Header>
      <Card.Title id="spend-caps-title" class="text-lg">Caps</Card.Title>
      <Card.Description>Empty or zero turns a cap off. OpenRouter's own key limits stay as a second safety net.</Card.Description>
    </Card.Header>
    <Card.Content>
      <form onsubmit={save} class="grid gap-5" novalidate>
        <div class="grid gap-4 sm:grid-cols-2">
          {@render capField("cap-daily", "Daily cap", data.help.daily_spend_cap_usd, () => draft!.daily, (text) => (draft!.daily = text), problems.daily)}
          {@render capField("cap-monthly", "Monthly cap", data.help.monthly_spend_cap_usd, () => draft!.monthly, (text) => (draft!.monthly = text), problems.monthly)}
        </div>
        <div class="flex items-center justify-between gap-3 rounded-xl border bg-raised/30 px-3.5 py-3">
          <span class="flex items-center gap-1">
            <label for="cap-owner-dm" class="text-sm">Caps apply to your DM too</label>
            <InfoGlyph label="About caps in your DM">{data.help.cap_owner_dm}</InfoGlyph>
          </span>
          <Switch id="cap-owner-dm" bind:checked={draft.ownerDm} disabled={saving} />
        </div>
        <fieldset class="grid gap-3">
          <legend class="mb-2 flex items-center gap-1 text-sm font-medium">
            Monthly limit by area <InfoGlyph label="About area limits">{data.help.area_caps}</InfoGlyph>
          </legend>
          <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {#each data.areas as area (area.area)}
              {@render capField(`cap-area-${area.area}`, area.label, "", () => draft!.areas[area.area], (text) => (draft!.areas[area.area] = text), problems[area.area])}
            {/each}
          </div>
        </fieldset>
        <div class="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={!valid || !changed || saving}>
            {#if saving}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…{:else}Save caps{/if}
          </Button>
          {#if changed}
            <Button type="button" variant="ghost" onclick={() => show(data)} disabled={saving}>Undo changes</Button>
          {/if}
        </div>
      </form>
    </Card.Content>
  </Card.Root>
{/if}

{#snippet capField(id: string, label: string, help: string, get: () => string, set: (text: string) => void, problem: string | undefined)}
  <div class="grid gap-1.5">
    <span class="flex items-center gap-1">
      <label for={id} class="text-sm">{label}</label>
      {#if help}<InfoGlyph label={`About the ${label.toLowerCase()}`}>{help}</InfoGlyph>{/if}
    </span>
    <div class="relative">
      <span class="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-sm text-muted-foreground" aria-hidden="true">$</span>
      <Input
        {id}
        type="text"
        inputmode="decimal"
        autocomplete="off"
        placeholder="Off"
        class="h-9 pl-7 font-mono"
        aria-invalid={problem ? true : undefined}
        aria-describedby={problem ? `${id}-problem` : undefined}
        disabled={saving}
        bind:value={get, set}
      />
    </div>
    {#if problem}<p id={`${id}-problem`} role="alert" class="text-sm text-coral">{problem}</p>{/if}
  </div>
{/snippet}
