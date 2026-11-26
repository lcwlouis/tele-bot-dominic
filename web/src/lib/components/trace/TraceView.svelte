<script lang="ts">
  import Download from "@lucide/svelte/icons/download";
  import type { TraceEvent, TracePart } from "$lib/api";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import JsonBlock from "./JsonBlock.svelte";

  // One turn's full audit (dom/tracer.py), in the order of the W2 list: arrival and triage, the exact prompt, every
  // model call, tools, output checks, delivery, and what ran after the turn. `parts` is the turn first, then its
  // `after` parts (or a single background part).
  let { parts, turnId = null }: { parts: TracePart[]; turnId?: number | null } = $props();

  const main = $derived(parts.find((part) => part.kind === "turn") ?? parts[0]);
  const later = $derived(parts.filter((part) => part !== main));
  const events = $derived(main?.events ?? []);
  const of = (types: string[], list: TraceEvent[] = events) => list.filter((event) => types.includes(event.type));

  const arrival = $derived(of(["arrival", "triage"]));
  const calls = $derived(of(["model_call"]));
  const chatCall = $derived(calls.find((call) => call.kind === "chat" || call.kind === "escalation"));
  const prompt = $derived(of(["prompt"]));
  const tools = $derived(of(["tool"]));
  const checks = $derived(of(["actions", "leak_check", "look"]));
  const delivery = $derived(of(["delivery", "reaction", "studio", "studio_step"]));
  const known = new Set(["arrival", "triage", "model_call", "prompt", "tool", "actions", "leak_check", "look", "delivery", "reaction", "studio", "studio_step"]);
  const other = $derived(events.filter((event) => !known.has(event.type)));

  const duration = (ms: unknown) => (typeof ms !== "number" ? "—" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
  const at = (event: TraceEvent) => `+${duration(event.t_ms)}`;
  const num = (value: unknown) => (typeof value === "number" ? value.toLocaleString("en-US") : "—");
  const words = (text: string) => text.replaceAll("_", " ").replace(/^./, (first) => first.toUpperCase());
  const str = (value: unknown) => (typeof value === "string" ? value : value === null || value === undefined ? "" : JSON.stringify(value));

  type Message = { role?: string; content?: unknown; tool_calls?: unknown };
  const messages = $derived(((chatCall?.request as { messages?: Message[] } | null)?.messages ?? []) as Message[]);
  const offered = $derived(((chatCall?.request as { tools?: { function?: { name?: string } }[] } | null)?.tools ?? []));
  function messageLabel(message: Message, index: number): string {
    const content = typeof message.content === "string" ? message.content : "";
    if (message.role === "system") return `${index + 1} · System message`;
    if (message.role === "user" && content.includes("<chat_window")) return `${index + 1} · Notes, chat window and turn note`;
    if (message.role === "tool") return `${index + 1} · Tool result`;
    if (message.role === "assistant") return `${index + 1} · Model's tool calls`;
    if (Array.isArray(message.content)) return `${index + 1} · ${message.role} with native media`;
    return `${index + 1} · ${words(message.role ?? "message")}`;
  }
  const size = (message: Message) => {
    const length = typeof message.content === "string" ? message.content.length : JSON.stringify(message.content ?? "").length;
    return `${length.toLocaleString("en-US")} characters`;
  };

  function reasoning(response: unknown): string | null {
    const message = (response as { choices?: { message?: Record<string, unknown> }[] } | null)?.choices?.[0]?.message;
    const text = message?.reasoning ?? message?.reasoning_content;
    return typeof text === "string" && text.trim() ? text : null;
  }
  const cost = (value: unknown) => (typeof value === "number" ? `$${value.toFixed(value < 0.01 ? 5 : 4)}` : null);
  const cacheShare = (call: TraceEvent) =>
    typeof call.input_tokens === "number" && call.input_tokens > 0 && typeof call.cached_tokens === "number"
      ? `${Math.round((100 * call.cached_tokens) / call.input_tokens)}% cached`
      : null;
  const totalCost = $derived(parts.flatMap((part) => part.events).reduce((sum, event) => sum + (typeof event.cost === "number" ? event.cost : 0), 0));
  const redactions = $derived(parts.reduce((sum, part) => sum + part.redactions, 0));
</script>

{#snippet line(event: TraceEvent, title: string, detail: string = "")}
  <div class="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-sm">
    <span class="w-16 shrink-0 font-mono text-caption text-muted-foreground tabular-nums">{at(event)}</span>
    <span class="font-medium">{title}</span>
    {#if detail}<span class="min-w-0 break-words text-muted-foreground">{detail}</span>{/if}
  </div>
{/snippet}

{#snippet section(title: string, count: number, about: string)}
  <h3 class="flex items-center gap-1.5 text-meta font-semibold">
    {title}<span class="font-normal text-muted-foreground">{count}</span>
    <InfoGlyph label={`About ${title.toLowerCase()}`}>{about}</InfoGlyph>
  </h3>
{/snippet}

{#if main}
  <div class="grid grid-cols-[minmax(0,1fr)] gap-5" data-trace>
    <div class="flex flex-wrap items-center gap-2 text-meta text-muted-foreground">
      <Badge variant="outline">{main.scope.replaceAll("_", " ")}</Badge>
      <span>{main.outcome || "Stayed quiet"}</span>
      <span>· {duration(main.duration_ms)}</span>
      {#if main.model}<span class="font-mono break-all">· {main.model}</span>{/if}
      {#if main.fallback}<Badge variant="destructive">Fallback</Badge>{/if}
      {#if totalCost > 0}<span>· {cost(totalCost)}</span>{/if}
      {#if redactions}<Badge variant="secondary">{redactions} redacted</Badge>{/if}
      {#if parts.some((part) => part.truncated)}<Badge variant="secondary">Cut to the size cap</Badge>{/if}
      {#if turnId !== null}
        <span class="ml-auto">
          <IconButton label="Export this turn's trace as JSON" icon={Download} href={`/api/traces/turn/${turnId}/export`} download="" variant="outline" />
        </span>
      {/if}
    </div>

    <section class="grid grid-cols-[minmax(0,1fr)] gap-2" aria-label="Arrival and triage">
      {@render section("Arrival and triage", arrival.length, "When the message came, Dom's presence, the queue waits, and Jev's scores and decisions.")}
      {#each arrival as event, index (index)}
        {#if event.type === "arrival"}
          {@render line(event, event.stage === "triage" ? "Arrived" : "Turn started", `${str(event.state)}${event.catch_up ? ", catching up" : ""}${typeof event.unread === "number" ? `, ${event.unread} unread` : ""}${event.resumed ? ", resumed" : ""}`)}
        {:else}
          {@render line(event, `Jev: ${words(str(event.mode))}`, event.verdict ? Object.entries(event.verdict as Record<string, unknown>).filter(([, value]) => value !== null && value !== "").map(([key, value]) => `${key} ${str(value)}`).join(", ") : event.urgency !== undefined ? `urgency ${str(event.urgency)}` : event.minutes !== undefined ? `stay ${str(event.minutes)} minutes` : "")}
        {/if}
      {/each}
      {#each main.visits as visit, index (index)}
        <p class="text-sm"><span class="font-medium">Attention:</span> {visit.reason}{visit.behind ? `, behind ${visit.behind}` : ""}, {duration(visit.duration_ms)}</p>
      {/each}
      {#if main.timings.length}
        <ul class="flex flex-wrap gap-1" aria-label="Stage timings">
          {#each main.timings as timing, index (index)}<li><Badge variant="outline" class="font-mono">{words(timing.what)} {duration(timing.ms)}</Badge></li>{/each}
        </ul>
      {/if}
      {#if arrival.length}<JsonBlock label="Arrival and triage events" value={arrival} />{/if}
    </section>

    <section class="grid grid-cols-[minmax(0,1fr)] gap-2" aria-label="Prompt">
      {@render section("Prompt", messages.length, "The exact messages the chat model got, the tools it was offered with their schemas, and how much of it the provider had cached.")}
      {#each prompt as event, index (index)}
        {@render line(event, "Fingerprint", `${str(event.fingerprint)} · ${num(event.messages)} messages · ${(event.tools as string[] | undefined)?.length ?? 0} tools`)}
      {/each}
      {#if chatCall}
        <p class="text-meta text-muted-foreground">
          System <code class="font-mono">{str(chatCall.system_hash)}</code>, tools <code class="font-mono">{str(chatCall.tools_hash)}</code>{#if chatCall.user_prefix_hash}, user prefix <code class="font-mono">{str(chatCall.user_prefix_hash)}</code>{/if}
          · {num(chatCall.cached_tokens)} of {num(chatCall.input_tokens)} input tokens cached
        </p>
        <div class="grid grid-cols-[minmax(0,1fr)] gap-1.5">
          {#each messages as message, index (index)}
            <JsonBlock label={messageLabel(message, index)} meta={size(message)} value={message.content ?? message} />
          {/each}
          {#if offered.length}
            <JsonBlock label={`Tools offered (${offered.length})`} meta={offered.map((tool) => tool.function?.name).join(", ")} value={offered} />
          {/if}
        </div>
      {:else}
        <p class="text-meta text-muted-foreground">No chat model call in this turn.</p>
      {/if}
    </section>

    <section class="grid grid-cols-[minmax(0,1fr)] gap-2" aria-label="Model calls">
      {@render section("Model calls", calls.length, "Every provider attempt: route, model, fallbacks, the full request and response, reasoning when the model returns it, tokens, cost and timing.")}
      {#each calls as call, index (index)}
        <div class="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-1.5 rounded-lg border bg-raised/40 p-2.5">
          {@render line(call, `${words(str(call.kind))}: ${str(call.model)}`, [call.host ? `on ${str(call.host)}` : "", call.fallback ? "fallback" : "", duration(call.duration_ms), call.status ? `status ${str(call.status)}` : "", `in ${num(call.input_tokens)}, out ${num(call.output_tokens)}`, cacheShare(call) ?? "", cost(call.cost) ?? ""].filter(Boolean).join(" · "))}
          {#if call.error}<p class="text-sm text-status-failed">{str(call.error)}</p>{/if}
          <JsonBlock label="Request" meta={str(call.url)} value={call.request} />
          {#if reasoning(call.response)}<JsonBlock label="Reasoning" value={reasoning(call.response)} />{/if}
          <JsonBlock label="Response" value={call.response} />
        </div>
      {/each}
    </section>

    <section class="grid grid-cols-[minmax(0,1fr)] gap-2" aria-label="Tools">
      {@render section("Tools", tools.length, "Each tool call with its arguments, result, errors and how long it took.")}
      {#each tools as tool, index (index)}
        <div class="grid grid-cols-[minmax(0,1fr)] gap-1.5">
          {@render line(tool, str(tool.name), `${duration(tool.duration_ms)}${tool.offered === false ? " · not offered" : ""}${tool.error ? " · error" : ""}`)}
          <JsonBlock label="Arguments" value={tool.arguments} />
          <JsonBlock label="Result" value={tool.result} meta={str(tool.result).slice(0, 80)} />
        </div>
      {/each}
    </section>

    <section class="grid grid-cols-[minmax(0,1fr)] gap-2" aria-label="Output checks">
      {@render section("Output checks", checks.length, "The answer the model gave, the leak scan's score against its cutoff, and the media look-before-send.")}
      {#each checks as event, index (index)}
        {#if event.type === "actions"}
          {@render line(event, "Final answer")}
          <JsonBlock label="Actions" value={event.actions} />
        {:else if event.type === "leak_check"}
          {@render line(event, "Leak scan", `score ${str(event.score)}${event.cutoff !== undefined ? ` against ${str(event.cutoff)}` : ""}${event.held ? ", held back" : ", passed"}${event.note ? ` (${str(event.note)})` : ""}`)}
        {:else}
          {@render line(event, `Look before send (${str(event.kind)})`, str(event.result))}
        {/if}
      {/each}
    </section>

    <section class="grid grid-cols-[minmax(0,1fr)] gap-2" aria-label="Delivery">
      {@render section("Delivery", delivery.length, "Each bubble and media item as sent, private-reply fallbacks, reactions, and studio jobs with their provenance.")}
      {#each delivery as event, index (index)}
        {#if event.type === "delivery"}
          {@render line(event, words(str(event.kind)), `${event.msg_id ? `#${str(event.msg_id)} ` : ""}${str(event.text)}`)}
        {:else if event.type === "reaction"}
          {@render line(event, `Reaction ${str(event.emoji)}`, str(event.result))}
        {:else}
          {@render line(event, event.type === "studio" ? "Studio job" : `Studio step: ${str(event.kind)}`, event.type === "studio" ? Object.keys((event.requested as object) ?? {}).join(", ") : `${str(event.model)} on ${str(event.host)}${event.redo ? " (redo)" : ""}`)}
        {/if}
      {/each}
    </section>

    {#if later.length || other.length}
      <section class="grid grid-cols-[minmax(0,1fr)] gap-2" aria-label="After the turn">
        {@render section("After the turn", later.length + other.length, "Work that ran once the reply was out: the staying-online check, studio jobs, the memory pass and its ops.")}
        {#each other as event, index (index)}
          {@render line(event, words(event.type))}
          <JsonBlock label={words(event.type)} value={event} />
        {/each}
        {#each later as part, index (index)}
          <div class="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-1.5 rounded-lg border bg-raised/40 p-2.5">
            <p class="text-sm font-medium">{words(part.label)} <span class="font-normal text-muted-foreground">· {duration(part.duration_ms)}</span></p>
            {#each part.events as event, eventIndex (eventIndex)}
              {#if event.type === "model_call"}
                {@render line(event, `${words(str(event.kind))}: ${str(event.model)}`, `${duration(event.duration_ms)}${cost(event.cost) ? ` · ${cost(event.cost)}` : ""}`)}
                <JsonBlock label="Request" value={event.request} />
                <JsonBlock label="Response" value={event.response} />
              {:else if event.type === "memory_op"}
                {@render line(event, `Memory: ${str((event.op as Record<string, unknown>)?.op)} ${str((event.op as Record<string, unknown>)?.file)}`, str(event.result))}
                <JsonBlock label="Op" value={event.op} />
              {:else}
                {@render line(event, words(event.type), event.text ? str(event.text) : "")}
              {/if}
            {/each}
          </div>
        {/each}
      </section>
    {/if}
  </div>
{/if}
