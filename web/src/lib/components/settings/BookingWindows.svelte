<script lang="ts">
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import Plus from "@lucide/svelte/icons/plus";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import { onMount } from "svelte";
  import { getJSON, send, type BookingView } from "$lib/api";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import { Button } from "$lib/components/ui/button";
  import { Input } from "$lib/components/ui/input";
  import { Switch } from "$lib/components/ui/switch";
  import { toast } from "$lib/toast.svelte";
  import { cn } from "$lib/utils";

  // Settings → Services → Google: when the owner is willing to meet friends (v0.8.6 W2). Dom offers a friend only times
  // inside these windows that are also free on the calendar, and asks the owner before every booking. No windows means no
  // booking. Saving replaces the whole list, as the owner DM's `set_windows` does.
  const PATH = "/api/integrations/google/booking";
  const DAYS = [
    ["mon", "Mon"],
    ["tue", "Tue"],
    ["wed", "Wed"],
    ["thu", "Thu"],
    ["fri", "Fri"],
    ["sat", "Sat"],
    ["sun", "Sun"],
  ] as const;

  let view = $state<BookingView | null>(null);
  let draft = $state<Pick<BookingView, "windows" | "notice_hours" | "ahead_days" | "invite_friends"> | null>(null);
  let saving = $state(false);
  let failed = $state(false);

  function adopt(next: BookingView) {
    view = next;
    draft = $state.snapshot({
      windows: next.windows.map((item) => ({ ...item, days: [...item.days] })),
      notice_hours: next.notice_hours,
      ahead_days: next.ahead_days,
      invite_friends: next.invite_friends,
    });
  }

  onMount(async () => {
    try {
      adopt(await getJSON<BookingView>(PATH));
    } catch {
      failed = true;
    }
  });

  const whole = (value: unknown, low: number, high: number) => Number.isInteger(value) && (value as number) >= low && (value as number) <= high;
  const valid = $derived(
    draft !== null &&
      draft.windows.every((item) => item.days.length > 0 && /^\d{2}:\d{2}$/.test(item.start) && /^\d{2}:\d{2}$/.test(item.end) && item.start < item.end) &&
      whole(draft.notice_hours, 0, 336) &&
      whole(draft.ahead_days, 1, 60),
  );

  function toggleDay(index: number, day: (typeof DAYS)[number][0]) {
    if (!draft) return;
    const item = draft.windows[index];
    item.days = item.days.includes(day) ? item.days.filter((d) => d !== day) : [...item.days, day];
  }

  async function save() {
    if (!draft || !valid) return;
    saving = true;
    try {
      const { note, ...next } = await send<BookingView & { note?: string }>(PATH, $state.snapshot(draft));
      adopt(next);
      toast.show("ok", note || (next.windows.length ? "Meeting windows saved." : "No windows: Dom books nothing."));
    } catch {
      // the toast says why
    } finally {
      saving = false;
    }
  }
</script>

<section class="grid gap-2" aria-labelledby="booking-title">
  <div class="flex items-center gap-1.5">
    <h4 id="booking-title" class="text-sm font-medium">Meeting windows</h4>
    <InfoGlyph label="About meeting windows">
      The days and times you are willing to meet friends. When a friend asks to meet, in a chat or a group, Dom offers only times inside
      these windows that are also free on your calendar and his, and shares nothing else about your calendar. Every booking asks you
      first, unless you set "Confirm a booking" to Allow in Access. With no windows Dom books nothing.
    </InfoGlyph>
  </div>

  {#if failed}
    <p class="text-sm text-coral">Couldn't load the meeting windows.</p>
  {:else if view && draft}
    {#if !view.ready}
      <p class="text-sm text-muted-foreground">
        Booking needs "Calendar: read events" and "Dom's own calendars" switched on above. You can set the windows now.
      </p>
    {:else if draft.windows.length && !view.calendar_made}
      <p class="text-sm text-muted-foreground">Dom will ask before he makes the calendar "{view.calendar}" where bookings go.</p>
    {/if}
    {#if draft.windows.length === 0}
      <p class="text-sm text-muted-foreground">No windows yet, so Dom books nothing. Times are in {view.zone}.</p>
    {/if}
    <ul class="grid gap-3">
      {#each draft.windows as item, index (index)}
        <li class="grid gap-2 rounded-md border p-2" aria-label={`Window ${index + 1}`}>
          <div class="flex flex-wrap gap-1" role="group" aria-label={`Days for window ${index + 1}`}>
            {#each DAYS as [id, label] (id)}
              <button
                type="button"
                aria-pressed={item.days.includes(id)}
                class={cn(
                  "rounded-md border px-2 py-1 text-sm",
                  item.days.includes(id) ? "border-primary bg-primary text-primary-foreground" : "bg-raised text-muted-foreground",
                )}
                onclick={() => toggleDay(index, id)}>{label}</button
              >
            {/each}
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <label class="flex items-center gap-1.5 text-sm"
              >From <Input class="h-9 w-28" type="time" required bind:value={item.start} aria-label={`Window ${index + 1} start`} /></label
            >
            <label class="flex items-center gap-1.5 text-sm"
              >to <Input class="h-9 w-28" type="time" required bind:value={item.end} aria-label={`Window ${index + 1} end`} /></label
            >
            <IconButton
              label={`Remove window ${index + 1}`}
              icon={Trash2}
              disabled={saving}
              onclick={() => draft && (draft.windows = draft.windows.filter((_, at) => at !== index))}
            />
          </div>
        </li>
      {/each}
    </ul>
    <div>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={saving || draft.windows.length >= 14}
        onclick={() => draft && draft.windows.push({ days: ["mon", "tue", "wed", "thu", "fri"], start: "19:00", end: "22:00" })}
      >
        <Plus aria-hidden="true" /> Add a window
      </Button>
    </div>
    <div class="flex items-center justify-between gap-3">
      <label for="booking-notice" class="text-sm">Least notice, in hours</label>
      <Input id="booking-notice" class="h-9 w-20" type="number" inputmode="numeric" min="0" max="336" step="1" required bind:value={draft.notice_hours} />
    </div>
    <div class="flex items-center justify-between gap-3">
      <label for="booking-ahead" class="text-sm">Furthest ahead, in days</label>
      <Input id="booking-ahead" class="h-9 w-20" type="number" inputmode="numeric" min="1" max="60" step="1" required bind:value={draft.ahead_days} />
    </div>
    <div class="flex items-center justify-between gap-3">
      <label for="booking-invite" class="text-sm">Invite the friend to the event when their email is known</label>
      <Switch id="booking-invite" checked={draft.invite_friends} onCheckedChange={(on) => draft && (draft.invite_friends = on)} />
    </div>
    <div>
      <Button type="button" size="sm" onclick={save} disabled={saving || !valid}>
        {#if saving}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…{:else}Save meeting windows{/if}
      </Button>
    </div>
  {:else}
    <p class="text-sm text-muted-foreground">Loading…</p>
  {/if}
</section>
