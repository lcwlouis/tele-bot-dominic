<script lang="ts">
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { onMount, untrack } from "svelte";
  import { getJSON, send, type StudioKind, type StudioLimits, type StudioLimitsView, type StudioScope, type StudioTier } from "$lib/api";
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

  const SCOPES: { scope: StudioScope; label: string }[] = [
    { scope: "owner_dm", label: "Your DM" }, { scope: "friend_dm", label: "Friends’ DMs" }, { scope: "group", label: "Groups" },
  ];
  const KINDS: Record<StudioKind, string> = {
    voice: "Voice notes", video_note: "Video notes", photo: "Photos", special: "Special videos",
  };
  const GAP_MAX = 10080; // a week: dom/config.py StudioTier.gap_minutes

  // One number per kind; video and photos also take a period. Per day sets <kind>_daily; per week clears it and sets
  // <kind>_weekly, as config.toml's rule has it (a daily limit wins when set).
  type Draft = {
    voice: number | null; video: number | null; videoWeek: boolean; image: number | null; imageWeek: boolean;
    special: number | null; gap: number | null;
    // A gap of a kind's own, spaced only from that kind; empty uses the general gap.
    voiceGap: number | null; videoGap: number | null; imageGap: number | null; specialGap: number | null;
  };

  let view = $state<StudioLimitsView | null>(null);
  let failed = $state("");
  let drafts = $state<Record<StudioScope, Draft> | null>(null);
  let daily = $state<number | null>(null);
  let saving = $state(false);
  // Any edit since the limits were last shown, valid or not: a reload must never overwrite it.
  let edited = $state(false);

  const toDraft = (tier: StudioTier): Draft => ({
    voice: tier.voice_daily,
    video: tier.video_daily ?? tier.video_weekly, videoWeek: tier.video_daily === null,
    image: tier.image_daily ?? tier.image_weekly, imageWeek: tier.image_daily === null,
    special: tier.special_daily, gap: tier.gap_minutes,
    voiceGap: tier.voice_gap_minutes, videoGap: tier.video_gap_minutes,
    imageGap: tier.image_gap_minutes, specialGap: tier.special_gap_minutes,
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

  onMount(load);

  const whole = (n: number | null, max = Infinity) => Number.isInteger(n) && n! >= 0 && n! <= max;
  const optionalGap = (n: number | null) => n === null || whole(n, GAP_MAX);
  const draftValid = (d: Draft) =>
    [d.voice, d.video, d.image, d.special].every((n) => whole(n)) && whole(d.gap, GAP_MAX) &&
    [d.voiceGap, d.videoGap, d.imageGap, d.specialGap].every(optionalGap);
  const valid = $derived(drafts !== null && whole(daily) && Object.values(drafts).every(draftValid));

  function tier(saved: StudioTier, d: Draft): StudioTier {
    return {
      ...saved,
      voice_daily: d.voice!, special_daily: d.special!, gap_minutes: d.gap!,
      voice_gap_minutes: d.voiceGap, video_gap_minutes: d.videoGap, image_gap_minutes: d.imageGap,
      special_gap_minutes: d.specialGap,
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

{#snippet gapField(id: string, label: string, value: number | null, set: (n: number | null) => void)}
  <div class="flex items-center justify-between gap-3">
    <label for={id} class="text-sm">{label}</label>
    <span class="flex items-center gap-2">
      <Input
        {id}
        class="h-9 w-20"
        type="number"
        inputmode="numeric"
        min="0"
        max={GAP_MAX}
        step="1"
        placeholder="general"
        aria-invalid={!optionalGap(value) || undefined}
        bind:value={() => value, set}
      />
      <span class="w-24 text-sm text-muted-foreground">minutes</span>
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

<Card.Root class="mt-6" role="region" aria-labelledby="media-limits-title">
  <Card.Header>
    <Card.Title id="media-limits-title" class="text-base">Media limits</Card.Title>
    <Card.Description>
      How many voice notes, video notes and photos Dom may send. The gap is the least time between two media in one chat;
      a kind can have a gap of its own.
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
      <section aria-labelledby="media-left-title" class="mb-5 rounded-xl border bg-surface p-3.5">
        <h3 id="media-left-title" class="kicker mb-2">Left now in your DM</h3>
        <ul class="flex flex-wrap gap-x-5 gap-y-1 text-sm">
          {#each owner.kinds as item (item.kind)}
            <li class={item.on ? "" : "text-muted-foreground"}>
              {KINDS[item.kind]}: <span class="font-mono">{item.left}</span>{item.on ? "" : " (off)"}
            </li>
          {/each}
        </ul>
        <p class="mt-2 text-sm text-muted-foreground">
          {nowNote(owner)}
          {#if owner.daily_left > 0}<span class="font-mono">{owner.daily_left}</span> left across all chats.{/if}
        </p>
      </section>

      <form onsubmit={save} oninput={() => (edited = true)} onchange={() => (edited = true)}>
        <div class="grid gap-5 md:grid-cols-3">
          {#each SCOPES as { scope, label } (scope)}
            {@const d = drafts[scope]}
            <fieldset class="grid content-start gap-2.5">
              <legend class="mb-2.5 font-medium">{label}</legend>
              {@render count(`limit-${scope}-voice`, KINDS.voice, d.voice, (n) => (d.voice = n), "per day")}
              {@render periodic(`limit-${scope}-video`, KINDS.video_note, d.video, (n) => (d.video = n), d.videoWeek, (w) => (d.videoWeek = w))}
              {@render periodic(`limit-${scope}-image`, KINDS.photo, d.image, (n) => (d.image = n), d.imageWeek, (w) => (d.imageWeek = w))}
              {#if scope === "owner_dm"}
                {@render count(`limit-${scope}-special`, KINDS.special, d.special, (n) => (d.special = n), "per day")}
              {/if}
              {@render count(`limit-${scope}-gap`, "Gap", d.gap, (n) => (d.gap = n), "minutes", GAP_MAX)}
              <details class="rounded-lg border px-3 py-2">
                <summary class="cursor-pointer text-sm">Gaps for one kind</summary>
                <div class="mt-2.5 grid gap-2.5">
                  {@render gapField(`limit-${scope}-voice-gap`, "Voice gap", d.voiceGap, (n) => (d.voiceGap = n))}
                  {@render gapField(`limit-${scope}-video-gap`, "Video gap", d.videoGap, (n) => (d.videoGap = n))}
                  {@render gapField(`limit-${scope}-image-gap`, "Photo gap", d.imageGap, (n) => (d.imageGap = n))}
                  {#if scope === "owner_dm"}
                    {@render gapField(`limit-${scope}-special-gap`, "Special gap", d.specialGap, (n) => (d.specialGap = n))}
                  {/if}
                  <p class="text-xs text-muted-foreground">
                    A kind with its own gap is spaced only from its last one, so a voice note and a photo can arrive together.
                    The rest share the general gap.
                  </p>
                </div>
              </details>
            </fieldset>
          {/each}
        </div>
        <div class="mt-5 border-t pt-4 md:w-[calc((100%-2.5rem)/3)]">
          {@render count("limit-daily", "All chats", daily, (n) => (daily = n), "per day")}
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
