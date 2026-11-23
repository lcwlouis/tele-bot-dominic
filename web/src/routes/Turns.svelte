<script lang="ts">
  import { onMount } from "svelte";
  import type { TurnsData } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import { Label } from "$lib/components/ui/label";
  import type { Destination, Tab } from "$lib/nav";
  import { WorkPage } from "$lib/work.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  const page = new WorkPage<TurnsData>("/api/system/turns");
  // The chat filter rides in the address (?chat=<id>), so a chat's page can link straight to its turns.
  let chat = $state(new URLSearchParams(location.search).get("chat") ?? "");
  let shown = $state(25); // the log keeps 200 turns; a page of them at a time keeps it light
  onMount(() => page.load(chat ? `?${new URLSearchParams({ chat })}` : ""));

  function reload() {
    shown = 25;
    const url = new URL(location.href);
    if (chat) url.searchParams.set("chat", chat);
    else url.searchParams.delete("chat");
    history.replaceState(history.state, "", url);
    page.load(`?${new URLSearchParams({ chat })}`);
  }
  const duration = (ms: number) => (ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
  const tokens = (value: number | null) => (value === null ? "—" : value);
  const words = (text: string) => text.replaceAll("_", " ").replace(/^./, (first) => first.toUpperCase());
  const held = (outcome: string) => outcome.startsWith("Held back") || outcome === "Turn failed";
  // Stage colours are classes, not inline styles (Dom's CSP allows none). Every segment is also named in its title
  // and in the chips under it.
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
  const select = "h-10 rounded-lg border border-input bg-transparent px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50";
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
  {#if data.choices.length}
    <div class="mb-5 grid w-fit gap-1.5">
      <Label for="turn-chat">Chat</Label>
      <select id="turn-chat" class={select} bind:value={chat} onchange={reload}>
        <option value="">All chats</option>
        {#each data.choices as choice (choice.id)}<option value={choice.id}>{choice.title}</option>{/each}
      </select>
    </div>
  {/if}

  {#if data.visits.length}
    <section aria-labelledby="attention" class="mb-6 rounded-xl border bg-surface p-4">
      <h2 id="attention" class="text-lg font-semibold">Attention loop</h2>
      <p class="mt-2 flex justify-between font-mono text-xs text-muted-foreground"><time>{data.visits[0].time}</time><time>{data.visits[data.visits.length - 1].time}</time></p>
      <ol class="mt-2 grid gap-1.5" aria-label="Attention visits">
        {#each data.visits as visit, index (index)}
          <li>
            <span class="text-sm">{visit.title} · {duration(visit.duration_ms)} · {visit.depth} ready</span>
            <svg viewBox="0 0 1000 10" preserveAspectRatio="none" class="h-2.5 w-full" aria-hidden="true">
              <rect width="1000" height="10" class="fill-raised" />
              <rect x={visit.left} width={visit.width} height="10" class="fill-signal" />
            </svg>
          </li>
        {/each}
      </ol>
      <h3 class="mt-5 mb-2 font-semibold">Cache on return</h3>
      <div class="overflow-x-auto">
        <table class="w-full min-w-[28rem] text-sm">
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
    </section>
  {/if}

  {#if data.cross_chat.length}
    <section aria-labelledby="cross-chat" class="mb-6 rounded-xl border bg-surface p-4">
      <h2 id="cross-chat" class="text-lg font-semibold">Cross-chat</h2>
      <ol class="mt-3 grid gap-2" aria-label="Cross-chat deliveries">
        {#each data.cross_chat as item, index (index)}
          <li>
            <details class="rounded-lg border bg-raised/40 px-3 py-2 text-sm">
              <summary class="cursor-pointer font-medium break-words">
                {words(item.kind)} · {item.source} → {item.target} · {item.status.replaceAll("_", " ")}{item.drop_reason ? ` (${item.drop_reason})` : ""}
              </summary>
              <p class="mt-2 text-muted-foreground">
                Created <time datetime={item.created_at}>{item.created_at}</time>
                {#if item.queued_at} · Queued <time datetime={item.queued_at}>{item.queued_at}</time>{/if}
                {#if item.sending_at} · Sending <time datetime={item.sending_at}>{item.sending_at}</time>{/if}
                · Updated <time datetime={item.updated_at}>{item.updated_at}</time>
              </p>
              <pre class="mt-2 max-h-64 overflow-auto rounded-lg border bg-surface p-3 font-mono text-[0.8rem] break-words whitespace-pre-wrap">{item.text}</pre>
            </details>
          </li>
        {/each}
      </ol>
    </section>
  {/if}

  {#if data.turns.length === 0}
    <StatusNote title={`No turns yet${data.chat ? " for this chat" : ""}`} detail="Dom replies when someone messages him." />
  {:else}
    {#if data.visible_stages.length}
      <ul class="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-label="Stage colours">
        {#each data.visible_stages as stage (stage)}
          <li class="flex items-center gap-1.5"><svg viewBox="0 0 10 10" class="size-2.5" aria-hidden="true"><rect width="10" height="10" class={fillOf(stage)} /></svg>{data.stages[stage]}</li>
        {/each}
      </ul>
    {/if}
    <ol class="grid gap-2" aria-label="Turns, newest first">
      {#each data.turns.slice(0, shown) as turn, index (index)}
        <li>
          <details class="rounded-xl border bg-surface px-4 py-3">
            <summary class="grid cursor-pointer gap-1.5 sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-baseline sm:gap-x-3">
              <time datetime={turn.at} title={turn.full_time} class="font-mono text-sm text-muted-foreground">{turn.time}</time>
              <span class="min-w-0 font-medium break-words {held(turn.outcome) ? 'text-coral' : ''}">{turn.title} · {turn.outcome}</span>
              <span class="font-mono text-xs text-muted-foreground">
                {#if turn.first_bubble_ms !== null && turn.last_bubble_ms !== null}
                  First bubble {duration(turn.first_bubble_ms)} · Last bubble {duration(turn.last_bubble_ms)}
                {:else}
                  Turn {duration(turn.duration_ms)}
                {/if}
              </span>
              {#if turn.segments.length}
                <svg viewBox={`0 0 ${turn.bar_ms} 10`} preserveAspectRatio="none" class="h-2.5 w-full sm:col-span-3" role="img" aria-label="Stage durations">
                  {#each turn.segments as segment, segmentIndex (segmentIndex)}
                    <rect x={segment.x} width={segment.ms} height="10" class={fillOf(segment.stage)}><title>{segment.label} {duration(segment.ms)}</title></rect>
                  {/each}
                </svg>
              {/if}
            </summary>
            <div class="mt-3 flex flex-wrap gap-1.5">
              {#each turn.steps as step, stepIndex (stepIndex)}<Badge variant="outline">{step === "web_search" ? "Search" : words(step)}</Badge>{/each}
              {#each turn.visits as visit, visitIndex (visitIndex)}
                <Badge variant="outline">Picked: {visit.reason}{visit.behind ? ` · Waited behind ${visit.behind}` : ""}{visit.capped ? ` · Skipped ${visit.capped} after two visits` : ""}</Badge>
              {/each}
              {#each turn.timing as item, timingIndex (timingIndex)}<Badge variant="outline">{item.label} {duration(item.ms)}</Badge>{/each}
              {#if turn.model}<Badge variant="outline">{turn.model}</Badge>{/if}
              {#if turn.fallback}<Badge variant="destructive">Fallback</Badge>{/if}
              <Badge variant="outline">{turn.calls} {turn.calls === 1 ? "call" : "calls"}</Badge>
            </div>
            {#if turn.model_calls.length}
              <ol class="mt-3 grid gap-2" aria-label="Model calls">
                {#each turn.model_calls as call, callIndex (callIndex)}
                  <li class="rounded-lg border bg-raised/40 px-3 py-2 text-sm">
                    <p class="break-words">
                      {data.stages[call.kind] ?? words(call.kind)} · {call.model}{call.host ? ` · ${call.host}` : ""}{call.fallback ? " · Fallback" : ""} · {duration(call.duration_ms)}{call.status ? ` · ${call.status}` : ""}
                    </p>
                    <p class="font-mono text-xs text-muted-foreground">
                      Input {tokens(call.input_tokens)} · Output {tokens(call.output_tokens)} · Cached {tokens(call.cached_tokens)} tokens
                    </p>
                    {#if call.gap_ms !== null && call.gap_ms !== undefined}
                      <p class="font-mono text-xs text-muted-foreground">Return after {duration(call.gap_ms)} · {call.between_tokens} intervening tokens</p>
                    {/if}
                    <p class="mt-1 font-mono text-xs break-all text-muted-foreground">
                      System <code>{call.system_hash}</code> · Tools <code>{call.tools_hash}</code>{#if call.user_hash} · User <code>{call.user_hash}</code>{/if}{#if call.user_prefix_hash} · User prefix <code>{call.user_prefix_hash}</code>{/if}
                    </p>
                  </li>
                {/each}
              </ol>
            {/if}
          </details>
        </li>
      {/each}
    </ol>
    {#if data.turns.length > shown}
      <Button variant="outline" class="mt-4" onclick={() => (shown += 50)}>Show more turns ({data.turns.length - shown} left)</Button>
    {/if}
  {/if}
{/if}
