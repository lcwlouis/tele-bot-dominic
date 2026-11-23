<script lang="ts">
  import Bell from "@lucide/svelte/icons/bell";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import Inbox from "@lucide/svelte/icons/inbox";
  import Monitor from "@lucide/svelte/icons/monitor";
  import Send from "@lucide/svelte/icons/send";
  import Sparkles from "@lucide/svelte/icons/sparkles";
  import { onMount } from "svelte";
  import { getJSON, type HomeData } from "$lib/api";
  import HourlyBars from "$lib/components/HourlyBars.svelte";
  import Orbit, { ACTIVITY_LABELS } from "$lib/components/Orbit.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Progress } from "$lib/components/ui/progress";

  const REFRESH_MS = 15_000; // the orbit follows Dom's activity this closely

  let data = $state<HomeData | null>(null);
  let failed = $state("");

  async function load() {
    try {
      data = await getJSON<HomeData>("/api/home");
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(() => {
    load();
    const timer = setInterval(() => document.visibilityState === "visible" && load(), REFRESH_MS);
    const onVisible = () => document.visibilityState === "visible" && load();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  });

  const money = (amount: number) => `$${amount.toFixed(2)}`;
  const stateWord = { online: "Online", asleep: "Asleep", offline: "Offline" } as const;
</script>

{#if data === null && failed}
  <PageHeader kicker="Overview" title="Home" />
  <StatusNote tone="error" title="Couldn't load Home" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="secondary" onclick={load}>Retry</Button>
  </StatusNote>
{:else if data === null}
  <PageHeader kicker="Overview" title="Home" subtitle="Loading what Dom has been up to…" />
  <div class="grid gap-4 lg:grid-cols-[1.45fr_1fr]" aria-hidden="true">
    {#each [0, 1, 2, 3] as index (index)}
      <div class="h-52 animate-pulse rounded-xl border bg-surface"></div>
    {/each}
  </div>
{:else if data.setup}
  <PageHeader kicker="Setup" title="Claim Dom in Telegram" subtitle="Once Dom knows his owner, reload this page." />
{:else}
  {@const home = data}
  <PageHeader
    kicker={`Overview / ${home.now.date}`}
    title={`${home.now.greeting}.`}
    subtitle={home.waiting.count
      ? `${home.waiting.count} ${home.waiting.count === 1 ? "thing is" : "things are"} waiting for you. Here's what Dom has been up to.`
      : "All quiet. Here's what Dom has been up to."}
  />

  {#if failed}
    <StatusNote tone="error" title="Couldn't refresh" detail={`Showing what Dom said at ${home.now.clock}.`} class="mb-4">
      <Button variant="ghost" size="sm" onclick={load}>Retry</Button>
    </StatusNote>
  {/if}

  <div class="grid gap-4 lg:grid-cols-[1.45fr_1fr]">
    <Card.Root class="orbit-grid" aria-labelledby="presence-title">
      <Card.Content class="flex items-center gap-4 sm:gap-8">
        <Orbit activity={home.activity} class="-my-2 size-28 sm:size-52" />
        <div class="min-w-0 flex-1">
          <p class="kicker text-signal">01 / Presence</p>
          <h2 id="presence-title" class="mt-2 text-[2.1rem] leading-none font-semibold tracking-tight">
            {stateWord[home.presence.state]}
          </h2>
          <p class="mt-2 text-sm text-muted-foreground">
            {home.presence.state === "online" ? home.presence.title.replace(/^Online /, "") + " · " : ""}{home.presence.detail}
          </p>
          <div class="mt-4 flex flex-wrap items-center gap-2">
            <Badge variant="secondary" aria-label={`Activity: ${ACTIVITY_LABELS[home.activity]}`}>
              <span class="size-1.5 rounded-full bg-signal" aria-hidden="true"></span>
              {ACTIVITY_LABELS[home.activity]}
            </Badge>
            {#if home.desktop.task}
              <Badge variant="outline"><Monitor aria-hidden="true" /> Desktop task</Badge>
            {/if}
          </div>
          {#if home.desktop.task}
            <p class="mt-3 line-clamp-2 text-sm text-muted-foreground">{home.desktop.task.brief}</p>
          {/if}
        </div>
      </Card.Content>
    </Card.Root>

    <Card.Root aria-labelledby="vibe-title">
      <Card.Content class="flex h-full flex-col">
        <div class="flex items-start justify-between gap-3">
          <p class="kicker">02 / Today's vibe</p>
          <Sparkles class="size-4 text-signal" aria-hidden="true" />
        </div>
        <h2 id="vibe-title" class="mt-4 text-2xl font-semibold tracking-tight">{home.vibe.title || "No preset"}</h2>
        {#if home.vibe.description}
          <blockquote class="mt-3 border-l-2 border-signal/60 pl-3.5 text-[0.95rem] leading-relaxed">
            {home.vibe.description}
          </blockquote>
        {/if}
        <div class="mt-auto flex flex-wrap items-center justify-between gap-3 pt-5">
          <p class="font-mono text-[0.7rem] tracking-[0.12em] text-muted-foreground uppercase">
            {#if home.vibe.mode === "locked"}Fixed by you{:else if home.vibe.mode === "auto" && home.vibe.title}Picked by Dom{home.vibe.since ? ` · ${home.vibe.since}` : ""}{:else}Dom picks his own{/if}
          </p>
          <Button href="/chats" variant="secondary" size="sm">Change vibe</Button>
        </div>
      </Card.Content>
    </Card.Root>

    <Card.Root aria-labelledby="waiting-title">
      <Card.Header>
        <Card.Title id="waiting-title" class="text-lg">Waiting for you</Card.Title>
        <Card.Action><Badge variant={home.waiting.count ? "secondary" : "outline"}>{home.waiting.count} pending</Badge></Card.Action>
      </Card.Header>
      <Card.Content>
        {#if home.waiting.items.length}
          <ul class="divide-y border-t">
            {#each home.waiting.items as item, index (index)}
              <li class="flex items-center gap-3 py-3">
                <span class="grid size-9 shrink-0 place-items-center rounded-lg border bg-raised text-signal" aria-hidden="true">
                  <Inbox class="size-4" />
                </span>
                <span class="min-w-0 flex-1">
                  <span class="block truncate font-medium">{item.label}</span>
                  <span class="block truncate text-sm text-muted-foreground">{item.detail}</span>
                </span>
                <Button href={home.waiting.href} variant="ghost" size="sm" aria-label={`Review: ${item.label}`}>
                  Review <ChevronRight data-icon="inline-end" aria-hidden="true" />
                </Button>
              </li>
            {/each}
          </ul>
          {#if home.waiting.count > home.waiting.items.length}
            <a href={home.waiting.href} class="mt-2 inline-block text-sm font-medium text-signal-strong underline-offset-4 hover:underline">
              See all {home.waiting.count}
            </a>
          {/if}
        {:else}
          <StatusNote title="Nothing waiting" detail="Proposals and approvals land here." />
        {/if}
      </Card.Content>
    </Card.Root>

    <Card.Root aria-labelledby="today-title">
      <Card.Header>
        <Card.Title id="today-title" class="text-lg">Today at a glance</Card.Title>
        <Card.Action><span class="font-mono text-xs text-muted-foreground">00:00–{home.now.clock}</span></Card.Action>
      </Card.Header>
      <Card.Content>
        <dl class="grid grid-cols-3 gap-3">
          <div class="flex flex-col">
            <dt class="text-sm text-muted-foreground">Turns</dt>
            <dd class="order-first text-[1.75rem] font-semibold tracking-tight tabular-nums">{home.today.turns}</dd>
          </div>
          <div class="flex flex-col">
            <dt class="text-sm text-muted-foreground">Replies</dt>
            <dd class="order-first text-[1.75rem] font-semibold tracking-tight tabular-nums">{home.today.replies}</dd>
          </div>
          <div class="flex flex-col">
            <dt class="text-sm text-muted-foreground">{home.today.cap ? `of ${money(home.today.cap)} cap` : "spent, no cap"}</dt>
            <dd class="order-first text-[1.75rem] font-semibold tracking-tight tabular-nums">{money(home.today.spend)}</dd>
            {#if home.today.cap}
              <dd class="mt-2">
                <Progress
                  value={Math.min(home.today.spend, home.today.cap)}
                  max={home.today.cap}
                  aria-label="Spend against today's cap"
                />
              </dd>
            {/if}
          </div>
        </dl>
        <HourlyBars counts={home.today.hourly} label="Usual activity by hour" />
        <div class="mt-4 flex items-center gap-3 border-t pt-3 text-sm">
          <Bell class="size-4 shrink-0 text-signal" aria-hidden="true" />
          <span class="min-w-0 flex-1 truncate">
            {#if home.today.next_reminder}
              <span class="font-mono text-xs text-muted-foreground">{home.today.next_reminder.when}</span>
              {home.today.next_reminder.text}
            {:else}
              <span class="text-muted-foreground">No reminders coming up</span>
            {/if}
          </span>
          <a href="/app/work/reminders" class="inline-flex items-center gap-0.5 font-medium text-signal-strong underline-offset-4 hover:underline">
            Reminders <ChevronRight class="size-4" aria-hidden="true" />
          </a>
        </div>
      </Card.Content>
    </Card.Root>

    <Card.Root class="lg:col-span-2" aria-labelledby="recent-title">
      <Card.Header>
        <Card.Title id="recent-title" class="text-lg">Recent replies</Card.Title>
        <Card.Action>
          <Button href="/app/chats" variant="ghost" size="sm">Open chats <ChevronRight data-icon="inline-end" aria-hidden="true" /></Button>
        </Card.Action>
      </Card.Header>
      <Card.Content>
        {#if home.recent.length}
          <ul class="divide-y border-t">
            {#each home.recent as row, index (index)}
              <li class="flex items-start gap-3 py-3">
                <span class="grid size-9 shrink-0 place-items-center rounded-lg border bg-raised text-signal" aria-hidden="true">
                  <Send class="size-4" />
                </span>
                <span class="min-w-0 flex-1">
                  <span class="block font-medium">{row.chat}</span>
                  <span class="line-clamp-2 text-sm text-muted-foreground">“{row.text}”</span>
                </span>
                <span class="pt-0.5 font-mono text-xs text-muted-foreground">{row.at}</span>
              </li>
            {/each}
          </ul>
        {:else}
          <StatusNote title="No replies yet" detail="Dom's latest reply in each chat shows here." />
        {/if}
      </Card.Content>
    </Card.Root>
  </div>
{/if}

