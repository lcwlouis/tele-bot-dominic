<script lang="ts">
  import { onMount } from "svelte";
  import type { Breakdown, UsageData, UsagePart } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import type { Destination, Tab } from "$lib/nav";
  import { cn } from "$lib/utils";
  import { WorkPage } from "$lib/work.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  const page = new WorkPage<UsageData>("/api/system/usage");
  onMount(() => page.load());

  const money = (amount: number) => `$${amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  // Fills are classes, not inline styles: Dom's CSP allows no style attributes. The tables carry every number.
  const fills: Record<string, string> = {
    chat: "fill-signal",
    triage: "fill-signal/45",
    media: "fill-amber",
    memory: "fill-coral",
    search: "fill-amber/50",
    reflection: "fill-coral/50",
    guard: "fill-signal-strong",
    desktop: "fill-muted-foreground",
    escalation: "fill-foreground",
    studio_image: "fill-chart-2",
    studio_voice: "fill-chart-5",
    studio_video: "fill-chart-1",
    embedding: "fill-chart-3",
    assistant: "fill-chart-4",
    news: "fill-amber/25",
    subscription: "fill-chart-3/50",
  };
  const fillOf = (kind: string) => fills[kind] ?? "fill-muted-foreground";
  // The segment whose breakdown shows under the chart: a click, a tap, or Enter or Space on a focused segment.
  let picked = $state<{ date: string; part: UsagePart } | null>(null);
  const pick = (date: string, part: UsagePart) =>
    (picked = picked?.date === date && picked.part.kind === part.kind ? null : { date, part });
  const keyPick = (event: KeyboardEvent, date: string, part: UsagePart) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pick(date, part);
    }
  };
  const count = (n: number) => n.toLocaleString("en-US");
  const stroke = (data: UsageData) => (data.tone === "signal" ? "stroke-coral" : data.tone === "amber" ? "stroke-amber" : "stroke-signal");
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#snippet breakdown(rows: Breakdown, heading: string, kinds: Record<string, string>)}
  {#if rows.length}
    <table class="w-full text-sm">
      <thead>
        <tr class="text-left text-muted-foreground">
          <th scope="col" class="py-2 pr-3 font-medium">{heading}</th>
          <th scope="col" class="px-3 py-2 text-right font-medium">Calls</th>
          <th scope="col" class="py-2 pl-3 text-right font-medium">Spend</th>
        </tr>
      </thead>
      <tbody>
        {#each rows as row (row.name)}
          <tr class="border-t">
            <th scope="row" class="py-2 pr-3 text-left font-normal break-all">{kinds[row.name] ?? row.name}</th>
            <td class="px-3 py-2 text-right font-mono">{row.calls}</td>
            <td class="py-2 pl-3 text-right font-mono">{money(row.spend)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <p class="text-sm text-muted-foreground">No calls recorded.</p>
  {/if}
{/snippet}

{#if page.data === null && page.failed}
  <StatusNote tone="error" title="Couldn't load usage" detail={`Dom's dashboard answered: ${page.failed}.`}>
    <Button variant="outline" onclick={() => page.load()}>Try again</Button>
  </StatusNote>
{:else if page.data === null}
  <div class="h-56 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  {@const data = page.data}
  <div class="grid gap-4 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
    <Card.Root>
      <Card.Header><Card.Title class="text-lg">Today</Card.Title></Card.Header>
      <Card.Content class="grid justify-items-center gap-1">
        <svg viewBox="-110 -110 220 120" class="w-full max-w-60" role="img" aria-label={`${money(data.spent)} spent${data.cap ? ` of a ${money(data.cap)} daily cap` : ""}`}>
          <path d="M-90 0 A90 90 0 0 1 90 0" class="stroke-line" fill="none" stroke-width="14" stroke-linecap="round" />
          {#if data.ratio}
            <path d="M-90 0 A90 90 0 0 1 90 0" pathLength="1" stroke-dasharray={`${data.ratio} 1`} class={stroke(data)} fill="none" stroke-width="14" stroke-linecap="round" />
          {/if}
        </svg>
        <p class="-mt-10 font-mono text-3xl font-semibold">{money(data.spent)}</p>
        <p class="text-sm text-muted-foreground">{data.cap ? `of ${money(data.cap)} daily cap` : "Daily cap disabled"}</p>
      </Card.Content>
    </Card.Root>

    <Card.Root>
      <Card.Header><Card.Title class="text-lg">Last 30 days</Card.Title></Card.Header>
      <Card.Content>
        <figure>
          <svg viewBox="0 0 300 72" preserveAspectRatio="none" class="h-40 w-full" role="group" aria-label={`30 days of spend by kind, ${data.fallbacks} fallback calls`}>
            {#each data.bars as day, index (day.date)}
              <g>
                <title>{day.date} · {money(day.spent)}{day.fallbacks ? ` · ${day.fallbacks} fallback calls` : ""}</title>
                <rect x={index * 10} y="68" width="7" height="2" class={index === data.bars.length - 1 ? "fill-signal" : "fill-line"} />
                {#each day.parts as part (part.kind)}
                  {@const on = picked?.date === day.date && picked.part.kind === part.kind}
                  <rect
                    x={index * 10}
                    y={part.y}
                    width="7"
                    height={Math.max(part.height, 0.8)}
                    role="button"
                    tabindex="0"
                    aria-pressed={on}
                    aria-label={`${day.date}, ${data.kinds[part.kind] ?? part.kind}: ${money(part.spend)}`}
                    class={cn(fillOf(part.kind), "cursor-pointer outline-none focus-visible:stroke-foreground", on && "stroke-foreground")}
                    stroke-width={on ? 2 : undefined}
                    vector-effect="non-scaling-stroke"
                    onclick={() => pick(day.date, part)}
                    onkeydown={(event) => keyPick(event, day.date, part)}
                  />
                {/each}
                {#if day.fallbacks}<rect x={index * 10} y="1" width="7" height="3" class="fill-coral" />{/if}
              </g>
            {/each}
          </svg>
          <ul class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-label="Chart key">
            {#each data.month_kinds as row (row.name)}
              <li class="flex items-center gap-1.5"><svg viewBox="0 0 10 10" class="size-2.5" aria-hidden="true"><rect width="10" height="10" class={fillOf(row.name)} /></svg>{data.kinds[row.name] ?? row.name}</li>
            {/each}
            {#if data.fallbacks}
              <li class="flex items-center gap-1.5"><svg viewBox="0 0 10 10" class="size-2.5" aria-hidden="true"><rect width="10" height="10" class="fill-coral" /></svg>{data.fallbacks} fallback calls</li>
            {/if}
          </ul>
          <p class="mt-2 text-xs text-muted-foreground">Tap a bar segment for its breakdown.</p>
        </figure>
        {#if picked}
          {@const part = picked.part}
          <section aria-live="polite" aria-label="Segment breakdown" class="mt-4 rounded-lg border bg-raised/40 p-3 text-sm">
            <div class="flex flex-wrap items-baseline justify-between gap-2">
              <h3 class="font-semibold">{data.kinds[part.kind] ?? part.kind} · {picked.date}</h3>
              <Button variant="ghost" size="xs" onclick={() => (picked = null)}>Close</Button>
            </div>
            <p class="mt-1 font-mono text-xs text-muted-foreground">
              {money(part.spend)} · {count(part.calls)} {part.calls === 1 ? "call" : "calls"} · {count(part.input_tokens)} in · {count(part.output_tokens)} out tokens
            </p>
            <div class="mt-2 overflow-x-auto">
              <table class="w-full min-w-[22rem]">
                <thead>
                  <tr class="text-left text-muted-foreground">
                    <th scope="col" class="py-1.5 pr-3 font-medium">Model</th>
                    <th scope="col" class="px-3 py-1.5 text-right font-medium">Spend</th>
                    <th scope="col" class="py-1.5 pl-3 text-right font-medium">Tokens in / out</th>
                  </tr>
                </thead>
                <tbody>
                  {#each part.models as row (row.model)}
                    <tr class="border-t">
                      <th scope="row" class="py-1.5 pr-3 text-left font-normal break-all">{row.model}</th>
                      <td class="px-3 py-1.5 text-right font-mono">{money(row.spend)}</td>
                      <td class="py-1.5 pl-3 text-right font-mono">{count(row.input_tokens)} / {count(row.output_tokens)}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          </section>
        {/if}
      </Card.Content>
    </Card.Root>
  </div>

  <div class="mt-4 grid gap-4 lg:grid-cols-2">
    <Card.Root>
      <Card.Content>{@render breakdown(data.today_kinds, "Today by kind", data.kinds)}</Card.Content>
    </Card.Root>
    <Card.Root>
      <Card.Content>
        <details>
          <summary class="cursor-pointer font-medium">Today by model</summary>
          <div class="mt-3">{@render breakdown(data.today_models, "Model", {})}</div>
        </details>
      </Card.Content>
    </Card.Root>
    <Card.Root class="lg:col-span-2">
      <Card.Content>
        <details>
          <summary class="cursor-pointer font-medium">Last 30 days by kind and model</summary>
          <div class="mt-3 grid gap-5 lg:grid-cols-2">
            {@render breakdown(data.month_kinds, "Kind", data.kinds)}
            {@render breakdown(data.month_models, "Model", {})}
          </div>
        </details>
      </Card.Content>
    </Card.Root>
  </div>
{/if}
