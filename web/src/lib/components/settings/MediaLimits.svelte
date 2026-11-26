<script lang="ts">
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { onMount, untrack } from "svelte";
  import { getJSON, send, type SpendView, type StudioKind, type StudioLimits, type StudioLimitsView, type StudioScope, type StudioTier } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import * as Select from "$lib/components/ui/select";
  import { toast } from "$lib/toast.svelte";

  // How often Dom may send studio media ([studio.limits]), and what is left now in the owner's DM. A save takes the
  // revision that came with the limits shown, and hands the page the one it returns; when another save on the page
  // moves `revision`, the limits load again unless they hold unsaved edits. The quota reads the limits on every turn,
  // so a save applies at once.
  let { revision = $bindable() }: { revision: string } = $props();

  const KINDS: Record<StudioKind, string> = {
    voice: "Voice notes", video_note: "Video notes", photo: "Photos", special: "Special videos",
  };
  const GAP_MAX = 10080; // a week: dom/config.py StudioTier.gap_minutes

  // One number per kind; video and photos also take a period. Per day sets <kind>_daily; per week clears it and sets
  // <kind>_weekly, as config.toml's rule has it (a daily limit wins when set).
  type Draft = {
    voice: number | null; video: number | null; videoWeek: boolean; image: number | null; imageWeek: boolean;
    special: number | null; gap: number | null;
  };

  let view = $state<StudioLimitsView | null>(null);
  let failed = $state("");
  let drafts = $state<Record<StudioScope, Draft> | null>(null);
  let daily = $state<number | null>(null);
  let saving = $state(false);
  // Cloud media spend this month against the media area's limit (E86), where /api/spend answers.
  let mediaSpend = $state<SpendView["areas"][number] | null>(null);
  // Any edit since the limits were last shown, valid or not: a reload must never overwrite it.
  let edited = $state(false);

  const toDraft = (tier: StudioTier): Draft => ({
    voice: tier.voice_daily,
    video: tier.video_daily ?? tier.video_weekly, videoWeek: tier.video_daily === null,
    image: tier.image_daily ?? tier.image_weekly, imageWeek: tier.image_daily === null,
    special: tier.special_daily, gap: tier.gap_minutes,
  });

  function show(loaded: StudioLimitsView) {
    view = loaded;
    drafts = { owner_dm: toDraft(loaded.limits.owner_dm), friend_dm: toDraft(loaded.limits.friend_dm), group: toDraft(loaded.limits.group) };
    daily = loaded.limits.daily;
    edited = false;
  }

  async function load() {
    try {
      const loaded = await getJSON<StudioLimitsView>("/api/studio/limits");
      if (!edited) show(loaded);
      // With edits in progress (even half-typed ones) the drafts stay. If another save left the limits as they were,
      // the editor just takes the new revision; if it changed them, saving gets a 409 rather than losing either.
      else if (view && JSON.stringify(loaded.limits) === JSON.stringify(view.limits)) view = loaded;
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(() => {
    load();
    getJSON<SpendView>("/api/spend")
      .then((spend) => (mediaSpend = spend.areas.find((area) => area.area === "media") ?? null))
      .catch(() => (mediaSpend = null)); // the limits still work without it
  });
  const money = (amount: number) => `$${amount.toFixed(2)}`;

  const whole = (n: number | null, max = Infinity) => Number.isInteger(n) && n! >= 0 && n! <= max;
  const draftValid = (d: Draft) => [d.voice, d.video, d.image, d.special].every((n) => whole(n)) && whole(d.gap, GAP_MAX);
  const valid = $derived(drafts !== null && whole(daily) && Object.values(drafts).every(draftValid));

  function tier(saved: StudioTier, d: Draft): StudioTier {
    return {
      ...saved,
      voice_daily: d.voice!, special_daily: d.special!, gap_minutes: d.gap!,
      video_daily: d.videoWeek ? null : d.video!, video_weekly: d.videoWeek ? d.video! : saved.video_weekly,
      image_daily: d.imageWeek ? null : d.image!, image_weekly: d.imageWeek ? d.image! : saved.image_weekly,
    };
  }

  const limits = $derived.by((): StudioLimits | null => {
    if (!view || !drafts || !valid) return null;
    const saved = view.limits;
    return {
      owner_dm: tier(saved.owner_dm, drafts.owner_dm), friend_dm: tier(saved.friend_dm, drafts.friend_dm),
      group: tier(saved.group, drafts.group), daily: daily!,
    };
  });
  const changed = $derived(limits !== null && JSON.stringify(limits) !== JSON.stringify(view!.limits));

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!changed || saving) return;
    saving = true;
    try {
      const result = await send<StudioLimitsView>("/api/studio/limits", { revision: view!.revision, limits });
      revision = result.revision;
      show(result);
      toast.show("ok", "Media limits saved.");
    } catch {
      // send() already showed why
    } finally {
      saving = false;
    }
  }

  $effect(() => {
    const current = revision;
    untrack(() => {
      if (view && current && current !== view.revision && !saving) void load();
    });
  });

  // Dom's own clock: the API sends each time with Dom's UTC offset, so the label reads the same on any device.
  const clock = (iso: string) => iso.slice(11, 16);
  const weekday = (iso: string) => new Date(iso.slice(0, 10) + "T00:00:00Z").toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" });
  // Across a clock change the offset differs from now's, so it is shown: 01:15 means two different times that night.
  const offset = (iso: string) => /(Z|[+-]\d\d:\d\d)$/.exec(iso)?.[1] ?? "";
  const nextLabel = (next: string, now: string) =>
    `Next media from ${next.slice(0, 10) === now.slice(0, 10) ? "" : weekday(next) + " "}${clock(next)}` +
    (offset(next) === offset(now) ? "" : ` (UTC${offset(next).replace("Z", "+00:00")})`);
  // What the owner's DM may get now: the first reason nothing can go, or when the next can.
  function nowNote(owner: StudioLimitsView["owner"]): string {
    const on = owner.kinds.filter((item) => item.on);
    if (owner.daily_left === 0) return "All chats have used the daily total.";
    if (owner.sending) return "Media is on its way.";
    if (!on.length) return "Media is off in your DM.";
    if (!on.some((item) => item.left > 0)) return "Nothing left until a limit resets.";
    return owner.next_at ? `${nextLabel(owner.next_at, owner.now)}.` : "Media can go now.";
  }
</script>

{#snippet count(id: string, label: string, value: number | null, set: (n: number | null) => void, unit: string, max?: number)}
  <div class="flex items-center justify-between gap-3">
    <label for={id} class="text-sm">{label}</label>
    <span class="flex items-center gap-2">
      <Input
        {id}
        class="h-9 w-20"
        type="number"
        inputmode="numeric"
        min="0"
        {max}
        step="1"
        required
        aria-invalid={!whole(value, max ?? Infinity) || undefined}
        bind:value={() => value, set}
      />
      <span class="w-24 text-sm text-muted-foreground">{unit}</span>
    </span>
  </div>
{/snippet}

{#snippet periodic(id: string, label: string, value: number | null, set: (n: number | null) => void, week: boolean, setWeek: (w: boolean) => void)}
  <div class="flex items-center justify-between gap-3">
    <label for={id} class="text-sm">{label}</label>
    <span class="flex items-center gap-2">
      <Input
        {id}
        class="h-9 w-20"
        type="number"
        inputmode="numeric"
        min="0"
        step="1"
        required
        aria-invalid={!whole(value) || undefined}
        bind:value={() => value, set}
      />
      <Select.Root type="single" value={week ? "week" : "day"} onValueChange={(choice) => setWeek(choice === "week")}>
        <Select.Trigger class="h-9 w-24" aria-label={`${label}: per day or per week`}>{week ? "per week" : "per day"}</Select.Trigger>
        <Select.Content>
          <Select.Item value="day" label="per day" />
          <Select.Item value="week" label="per week" />
        </Select.Content>
      </Select.Root>
    </span>
  </div>
{/snippet}

{#snippet scopeFields(scope: StudioScope, label: string)}
  {@const d = drafts![scope]}
  <fieldset class="grid content-start gap-2.5">
    <legend class="mb-2.5 font-medium">{label}</legend>
    {@render count(`limit-${scope}-voice`, KINDS.voice, d.voice, (n) => (d.voice = n), "per day")}
    {@render periodic(`limit-${scope}-video`, KINDS.video_note, d.video, (n) => (d.video = n), d.videoWeek, (w) => (d.videoWeek = w))}
    {@render periodic(`limit-${scope}-image`, KINDS.photo, d.image, (n) => (d.image = n), d.imageWeek, (w) => (d.imageWeek = w))}
    {#if scope === "owner_dm"}
      {@render count(`limit-${scope}-special`, KINDS.special, d.special, (n) => (d.special = n), "per day")}
    {/if}
    {@render count(`limit-${scope}-gap`, "Gap", d.gap, (n) => (d.gap = n), "minutes", GAP_MAX)}
  </fieldset>
{/snippet}

<Card.Root class="mt-6" role="region" aria-labelledby="media-limits-title">
  <Card.Header>
    <Card.Title id="media-limits-title" class="text-base">Media limits</Card.Title>
    <Card.Description class="flex items-center gap-1">
      How many voice notes, video notes and photos Dom may send.
      <InfoGlyph label="About the gap">The gap is the least time between any two media in one chat.</InfoGlyph>
    </Card.Description>
  </Card.Header>
  <Card.Content>
    {#if failed && !view}
      <StatusNote tone="error" title="Couldn't load the media limits" detail={`Dom's dashboard answered: ${failed}.`}>
        <Button variant="secondary" onclick={load}>Retry</Button>
      </StatusNote>
    {:else if !view || !drafts}
      <p role="status" class="text-sm text-muted-foreground">Loading…</p>
    {:else}
      {@const owner = view.owner}
      <form onsubmit={save} oninput={() => (edited = true)} onchange={() => (edited = true)}>
        <div class="grid gap-5 md:grid-cols-3">
          <div class="grid content-start gap-5">
            <fieldset class="grid content-start gap-2.5 rounded-xl border bg-surface p-3.5">
              <legend class="float-left mb-2.5 font-medium">Global limits</legend>
              {@render count("limit-daily", "All chats", daily, (n) => (daily = n), "per day")}
              <p class="text-sm text-muted-foreground">
                {#if owner.daily_left > 0}<span class="font-mono">{owner.daily_left}</span> left today across all chats.{:else}All chats have used today's total.{/if}
              </p>
              {#if mediaSpend}
                <p class="text-sm text-muted-foreground" data-media-spend>
                  Cloud media this month: <span class="font-mono">{money(mediaSpend.spent)}</span>{#if mediaSpend.cap !== null}{" "}of
                  <span class="font-mono">{money(mediaSpend.cap)}</span> (<span class="font-mono">{money(mediaSpend.left ?? 0)}</span>{" "}left){:else}, no limit{/if}.
                </p>
              {/if}
            </fieldset>
            {@render scopeFields("owner_dm", "Your DM")}
            <section aria-labelledby="media-left-title" class="rounded-xl border bg-surface p-3.5">
              <h3 id="media-left-title" class="kicker mb-2">Left now in your DM</h3>
              <ul class="grid gap-1 text-sm">
                {#each owner.kinds as item (item.kind)}
                  <li class="flex justify-between gap-3 {item.on ? '' : 'text-muted-foreground'}">
                    <span>{KINDS[item.kind]}</span><span><span class="font-mono">{item.left}</span>{item.on ? "" : " (off)"}</span>
                  </li>
                {/each}
              </ul>
              <p class="mt-2 text-sm text-muted-foreground">{nowNote(owner)}</p>
            </section>
          </div>
          {@render scopeFields("friend_dm", "Friends’ DMs")}
          {@render scopeFields("group", "Groups")}
        </div>
        <div class="mt-4 flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={!changed || saving}>
            {#if saving}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…{:else}Save <span class="sr-only">media limits</span>{/if}
          </Button>
          <p class={["text-sm", valid ? "text-muted-foreground" : "text-coral"]} role={valid ? undefined : "alert"}>
            {valid ? "Applies at once." : `Enter whole numbers from 0. The gap is at most ${GAP_MAX} minutes (a week).`}
          </p>
        </div>
      </form>
    {/if}
  </Card.Content>
</Card.Root>
