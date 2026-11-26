<script lang="ts">
  import ChevronDown from "@lucide/svelte/icons/chevron-down";
  import ScrollText from "@lucide/svelte/icons/scroll-text";
  import { onMount } from "svelte";
  import type { Turn, TurnsData } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import AppPicker from "$lib/components/kit/AppPicker.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import StripList from "$lib/components/kit/StripList.svelte";
  import type { Status } from "$lib/components/kit/status";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import type { Destination, Tab } from "$lib/nav";
  import { WorkPage } from "$lib/work.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  const page = new WorkPage<TurnsData>("/api/system/turns");
  // The chat filter rides in the address (?chat=<id>), so a chat's page can link straight to its turns.
  let chat = $state(new URLSearchParams(location.search).get("chat") ?? "");
  let selected = $state<string | null>(null);
  onMount(() => page.load(chat ? `?${new URLSearchParams({ chat })}` : ""));

  function reload(next: string) {
    chat = next;
    selected = null;
    const url = new URL(location.href);
    if (chat) url.searchParams.set("chat", chat);
    else url.searchParams.delete("chat");
    history.replaceState(history.state, "", url);
    page.load(`?${new URLSearchParams({ chat })}`);
  }

  type Row = Turn & { key: string; status: Status };
  // A turn's status from what it did: held back (a guard or the budget), failed, paused, or done.
  function statusOf(outcome: string): Status {
    if (outcome.startsWith("Held back") || outcome === "Provider budget exhausted" || outcome === "Flood limited") return "held";
    if (outcome === "Turn failed" || outcome === "Reaction failed" || outcome === "Interrupted") return "failed";
    if (outcome === "Paused") return "paused";
    return "done";
  }
  const rows = $derived<Row[]>((page.data?.turns ?? []).map((turn) => ({ ...turn, key: String(turn.id), status: statusOf(turn.outcome) })));
  const options = $derived([{ value: "", label: "All chats" }, ...(page.data?.choices ?? []).map((choice) => ({ value: choice.id, label: choice.title }))]);

  const duration = (ms: number) => (ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
  const tokens = (value: number | null) => (value === null ? "unknown" : value.toLocaleString("en-US"));
  const words = (text: string) => text.replaceAll("_", " ").replace(/^./, (first) => first.toUpperCase());
  const queueWait = (turn: Turn) => turn.segments.find((segment) => segment.stage === "queue_wait")?.ms ?? null;
  const cached = (turn: Turn) => {
    const measured = turn.model_calls.filter((call) => call.input_tokens && call.cached_tokens !== null);
    const input = measured.reduce((sum, call) => sum + (call.input_tokens ?? 0), 0);
    return input ? Math.round((100 * measured.reduce((sum, call) => sum + (call.cached_tokens ?? 0), 0)) / input) : null;
  };
  // Stage colours are classes, not inline styles (Dom's CSP allows none). Every segment is also named in its title
  // and in the list under it.
  const fills: Record<string, string> = {
    away_wait: "fill-muted-foreground/50",
    debounce: "fill-line",
    media_wait: "fill-amber/50",
    lock_wait: "fill-coral/50",
    queue_wait: "fill-coral",
    triage: "fill-signal/45",
    model: "fill-signal",
    retry_backoff: "fill-amber",
    tool: "fill-signal-strong",
    guard: "fill-foreground/60",
    typing: "fill-muted-foreground",
    send: "fill-foreground",
    other: "fill-line",
    after_reply: "fill-muted-foreground/30",
  };
  const fillOf = (stage: string) => fills[stage] ?? "fill-muted-foreground";
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#if page.data === null && page.failed}
  <StatusNote tone="error" title="Couldn't load the turn log" detail={`Dom's dashboard answered: ${page.failed}.`}>
    <Button variant="outline" onclick={() => page.load()}>Try again</Button>
  </StatusNote>
{:else if page.data === null}
  <div class="h-56 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  {@const data = page.data}
  {#if data.visits.length || data.cross_chat.length}
    <!-- What spans turns: full-width sections, stacked and folded, so the turns lead the page. -->
    <div class="mb-5 grid gap-3">
      {#if data.visits.length}
        {@const longest = Math.max(...data.visits.map((visit) => visit.duration_ms), 1)}
        <details class="group rounded-xl border bg-surface">
          <summary class="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-xl px-4 font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
            <ChevronDown class="size-4 shrink-0 text-muted-foreground transition-transform duration-(--motion-quick) group-open:rotate-180" aria-hidden="true" />
            Attention loop <span class="text-meta font-normal text-muted-foreground">{data.visits.length} visits</span>
          </summary>
          <section aria-label="Attention loop" class="grid grid-cols-[minmax(0,1fr)] gap-5 border-t px-4 pt-3 pb-4">
            <!-- Rows, newest first; the bar is the visit's length against the longest one, not a place in time. -->
            <div class="max-h-[26rem] overflow-y-auto overscroll-contain rounded-lg border">
              <table class="w-full table-fixed text-sm">
                <caption class="sr-only">Attention visits, newest first</caption>
                <colgroup>
                  <col class="w-[6.75rem] sm:w-[9.5rem]" />
                  <col />
                  <col class="w-[4.75rem] sm:w-[42%]" />
                  <col class="w-12 sm:w-14" />
                </colgroup>
                <thead class="sticky top-0 bg-surface">
                  <tr class="text-left text-meta text-muted-foreground">
                    <th scope="col" class="py-2 pr-2 pl-3 font-medium">When</th>
                    <th scope="col" class="px-2 py-2 font-medium">Chat</th>
                    <th scope="col" class="px-2 py-2 font-medium">Stayed</th>
                    <th scope="col" class="py-2 pr-3 pl-2 text-right font-medium">Ready</th>
                  </tr>
                </thead>
                <tbody>
                  {#each [...data.visits].reverse() as visit, index (index)}
                    <tr class="border-t">
                      <td class="py-1.5 pr-2 pl-3 font-mono text-meta whitespace-nowrap text-muted-foreground tabular-nums">
                        <time>{visit.time.slice(0, -3)}<span class="max-sm:hidden">{visit.time.slice(-3)}</span></time>
                      </td>
                      <th scope="row" class="truncate px-2 py-1.5 text-left font-normal" title={visit.title}>{visit.title}</th>
                      <td class="px-2 py-1.5">
                        <span class="flex items-center gap-2">
                          <span class="shrink-0 tabular-nums sm:w-14 sm:text-right">{duration(visit.duration_ms)}</span>
                          <svg viewBox="0 0 100 6" preserveAspectRatio="none" class="hidden h-1.5 min-w-0 flex-1 rounded-full sm:block" aria-hidden="true">
                            <rect width="100" height="6" class="fill-raised" />
                            <rect width={Math.max(1, (100 * visit.duration_ms) / longest)} height="6" class="fill-signal" />
                          </svg>
                        </span>
                      </td>
                      <td class="py-1.5 pr-3 pl-2 text-right tabular-nums">{visit.depth}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
            <div>
              <h3 class="mb-2 flex items-center gap-1.5 font-semibold">
                Cache on return
                <InfoGlyph label="About cache on return">How much of the prompt the provider had cached when Dom came back to a chat, by how long he was away.</InfoGlyph>
              </h3>
              <div class="overflow-x-auto">
                <table class="w-full min-w-[24rem] text-sm">
                  <thead>
                    <tr class="text-left text-muted-foreground">
                      <th scope="col" class="py-2 pr-3 font-medium">Gap</th>
                      <th scope="col" class="px-3 py-2 font-medium">Returns</th>
                      <th scope="col" class="px-3 py-2 font-medium">Cache hit rate</th>
                      <th scope="col" class="py-2 pl-3 font-medium">Median intervening tokens</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each data.cache as row (row.gap)}
                      <tr class="border-t">
                        <th scope="row" class="py-2 pr-3 text-left font-normal">{row.gap}</th>
                        <td class="px-3 py-2 font-mono">{row.returns}</td>
                        <td class="px-3 py-2 font-mono">{row.hit === null ? "—" : `${row.hit.toFixed(1)}%`}</td>
                        <td class="py-2 pl-3 font-mono">{row.between ?? "—"}</td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            </div>
          </section>
        </details>
      {/if}
      {#if data.cross_chat.length}
        <details class="group rounded-xl border bg-surface">
          <summary class="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-xl px-4 font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
            <ChevronDown class="size-4 shrink-0 text-muted-foreground transition-transform duration-(--motion-quick) group-open:rotate-180" aria-hidden="true" />
            Cross-chat <span class="text-meta font-normal text-muted-foreground">{data.cross_chat.length} deliveries</span>
          </summary>
          <ol class="grid gap-2 border-t px-4 pt-3 pb-4" aria-label="Cross-chat deliveries">
            {#each data.cross_chat as item, index (index)}
              <li class="rounded-lg border bg-raised/40 px-3 py-2 text-sm">
                <p class="font-medium break-words">
                  {words(item.kind)} from {item.source} to {item.target}: {item.status.replaceAll("_", " ")}{item.drop_reason ? ` (${item.drop_reason})` : ""}
                </p>
                <p class="mt-1 text-meta text-muted-foreground">
                  Created <time datetime={item.created_at}>{item.created_at}</time>{#if item.queued_at}, queued <time datetime={item.queued_at}>{item.queued_at}</time>{/if}{#if item.sending_at}, sending <time datetime={item.sending_at}>{item.sending_at}</time>{/if}, updated <time datetime={item.updated_at}>{item.updated_at}</time>
                </p>
                <pre class="mt-2 max-h-64 overflow-auto rounded-lg border bg-surface p-3 font-mono text-[0.8rem] break-words whitespace-pre-wrap">{item.text}</pre>
              </li>
            {/each}
          </ol>
        </details>
      {/if}
    </div>
  {/if}

  <StripList
    items={rows}
    key={(row) => row.key}
    strip={(row) => ({ status: row.status, title: row.outcome, at: row.at, meta: row.title })}
    detailTitle={(row) => `${row.outcome} in ${row.title}`}
    label="Turns, newest first"
    bind:selected
    search="Filter turns"
    statuses={["held", "failed"]}
    level={2}
    name="Turns"
  >
    {#snippet toolbar()}
      {#if data.choices.length}<AppPicker label="Chat" {options} value={chat} onchange={reload} class="w-full sm:w-56 [&>label]:sr-only" />{/if}
    {/snippet}
    {#snippet details(turn)}
      {@const wait = queueWait(turn)}
      {@const hit = cached(turn)}
      {#if turn.has_trace}
        <a
          href={turn.chat_id === data.owner_chat ? `/app/talk?trace=1&turn=${turn.id}` : `${tab.href}/${turn.id}`}
          class="mb-3 inline-flex items-center gap-1.5 text-meta font-medium text-signal-strong underline-offset-4 hover:underline"
        ><ScrollText class="size-3.5" aria-hidden="true" />Open the full trace</a>
      {:else}
        <p class="mb-3 text-meta text-muted-foreground">No full trace: it ran before tracing, or is older than the trace retention.</p>
      {/if}
      <dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-body">
        <dt class="text-muted-foreground">When</dt>
        <dd>{turn.full_time}</dd>
        {#if turn.first_bubble_ms !== null && turn.last_bubble_ms !== null}
          <dt class="text-muted-foreground">Bubbles</dt>
          <dd class="tabular-nums">First after {duration(turn.first_bubble_ms)}, last after {duration(turn.last_bubble_ms)}</dd>
        {:else}
          <dt class="text-muted-foreground">Took</dt>
          <dd class="tabular-nums">{duration(turn.duration_ms)}</dd>
        {/if}
        {#if wait !== null}<dt class="text-muted-foreground">Queue wait</dt><dd class="tabular-nums">{duration(wait)}</dd>{/if}
        {#if turn.model}
          <dt class="text-muted-foreground">Model</dt>
          <dd class="flex flex-wrap items-center gap-1.5 font-mono text-meta break-all">{turn.model}{#if turn.fallback}<Badge variant="destructive">Fallback</Badge>{/if}</dd>
        {/if}
        <dt class="text-muted-foreground">Calls</dt>
        <dd class="tabular-nums">{turn.calls}{hit !== null ? `, ${hit}% of the prompt cached` : ""}</dd>
        {#if turn.steps.length}
          <dt class="text-muted-foreground">Tools</dt>
          <dd class="flex flex-wrap gap-1">{#each turn.steps as step, index (index)}<Badge variant="outline">{step === "web_search" ? "Search" : words(step)}</Badge>{/each}</dd>
        {/if}
        {#each turn.visits as visit, index (index)}
          <dt class="text-muted-foreground">Picked</dt>
          <dd>{visit.reason}{visit.behind ? `, waited behind ${visit.behind}` : ""}{visit.capped ? `, skipped ${visit.capped} after two visits` : ""}</dd>
        {/each}
      </dl>

      {#if turn.segments.length}
        <h3 class="mt-4 text-meta font-medium text-muted-foreground">Where the time went</h3>
        <svg viewBox={`0 0 ${turn.bar_ms} 10`} preserveAspectRatio="none" class="mt-1.5 h-2.5 w-full rounded-sm" role="img" aria-label="Stage durations">
          {#each turn.segments as segment, index (index)}
            <rect x={segment.x} width={segment.ms} height="10" class={fillOf(segment.stage)}><title>{segment.label} {duration(segment.ms)}</title></rect>
          {/each}
        </svg>
        <ul class="mt-2 grid gap-1 text-meta" aria-label="Stages">
          {#each turn.timing as item, index (index)}
            <li class="flex items-center gap-2 tabular-nums">
              <span class="min-w-0 flex-1">{item.label}</span><span class="text-muted-foreground">{duration(item.ms)}</span>
            </li>
          {/each}
        </ul>
        <ul class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-caption text-muted-foreground" aria-label="Stage colours">
          {#each turn.segments as segment, index (index)}
            <li class="flex items-center gap-1.5"><svg viewBox="0 0 10 10" class="size-2.5" aria-hidden="true"><rect width="10" height="10" class={fillOf(segment.stage)} /></svg>{segment.label}</li>
          {/each}
        </ul>
      {/if}

      {#if turn.model_calls.length}
        <h3 class="mt-4 text-meta font-medium text-muted-foreground">Model calls</h3>
        <ol class="mt-1.5 grid gap-2" aria-label="Model calls">
          {#each turn.model_calls as call, index (index)}
            <li class="rounded-lg border bg-raised/40 px-3 py-2 text-sm">
              <p class="break-words">
                {data.stages[call.kind] ?? words(call.kind)}: {call.model}{call.host ? ` on ${call.host}` : ""}{call.fallback ? ", fallback" : ""}, {duration(call.duration_ms)}{call.status ? `, ${call.status}` : ""}
              </p>
              <p class="text-meta text-muted-foreground tabular-nums">
                Input {tokens(call.input_tokens)}, output {tokens(call.output_tokens)}, cached {tokens(call.cached_tokens)} tokens
              </p>
              {#if call.gap_ms !== null && call.gap_ms !== undefined}
                <p class="text-meta text-muted-foreground tabular-nums">Came back after {duration(call.gap_ms)}, {call.between_tokens} intervening tokens</p>
              {/if}
              <p class="mt-1 flex flex-wrap items-center gap-x-2 font-mono text-xs break-all text-muted-foreground">
                <span class="font-sans">Prompt fingerprint</span>
                <span>system <code>{call.system_hash}</code></span>
                <span>tools <code>{call.tools_hash}</code></span>
                {#if call.user_hash}<span>user <code>{call.user_hash}</code></span>{/if}
                {#if call.user_prefix_hash}<span>user prefix <code>{call.user_prefix_hash}</code></span>{/if}
              </p>
            </li>
          {/each}
        </ol>
      {/if}
    {/snippet}
    {#snippet empty()}<StatusNote title={`No turns yet${data.chat ? " for this chat" : ""}`} detail="Dom replies when someone messages him." />{/snippet}
  </StripList>
{/if}
