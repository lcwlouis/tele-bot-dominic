<script lang="ts">
  import Monitor from "@lucide/svelte/icons/monitor";
  import type { MessageTurn, TalkMessage } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { cn } from "$lib/utils";

  // One message as a bubble, the Talk page's way: Dom's on the left, the owner's on the right, at a readable width,
  // with its time, a glyph when the owner wrote it on the dashboard and, on Dom's when known, an (i) with the turn
  // that sent it (model, tokens, cost, time taken, lane). Talk and Chats → Your DM share it.
  let {
    message,
    time,
    turn = null,
  }: {
    message: Pick<TalkMessage, "mine" | "who" | "text" | "origin" | "edited" | "media"> & { at: string };
    /** The time as shown under the text; `message.at` is the exact one, for the time element. */
    time: string;
    turn?: MessageTurn | null;
  } = $props();

  const count = new Intl.NumberFormat();
  const seconds = (ms: number) => `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
  const cost = (usd: number) => (usd === 0 ? "$0" : usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`);
  const tokens = (value: number | null) => (value === null ? "?" : count.format(value));
  const lanes: Record<string, string> = { fast: "Fast lane", main: "Main lane" };
</script>

<div class={cn("flex", message.mine ? "justify-start" : "justify-end")}>
  <div class={cn("bubble max-w-[min(85%,36rem)] min-w-0 rounded-2xl px-3.5 py-2", message.mine ? "rounded-bl-md bg-raised" : "rounded-br-md bg-tint")}>
    <span class="sr-only">{message.who}{message.origin === "dashboard" ? ", from the dashboard" : ""}:</span>
    {#if message.media?.url && message.media.kind === "photo"}
      <img src={message.media.url} alt={message.media.description ?? "A photo Dom sent"} class="mb-1.5 max-h-72 rounded-lg" loading="lazy" />
    {:else if message.media?.url && message.media.kind === "voice"}
      <audio controls src={message.media.url} class="mb-1.5 max-w-full" aria-label="Voice note from Dom"></audio>
    {:else if message.media?.url}
      <!-- svelte-ignore a11y_media_has_caption -->
      <video controls src={message.media.url} class="mb-1.5 max-h-72 max-w-full rounded-full" aria-label="Round video from Dom"></video>
    {:else if message.media}
      <Badge variant="outline" class="mb-1 max-w-full truncate">{message.media.kind}{message.media.description ? `: ${message.media.description}` : ""}</Badge>
    {/if}
    {#if message.text && !(message.media && message.text === `[${message.media.kind}]`)}
      <p class="text-body break-words whitespace-pre-wrap [overflow-wrap:anywhere]">{message.text}</p>
    {/if}
    <p class="mt-0.5 flex items-center justify-end gap-1 text-caption text-muted-foreground">
      {#if message.origin === "dashboard"}<span title="Sent from the dashboard" class="inline-flex" aria-hidden="true"><Monitor class="size-3" /></span>{/if}
      {#if message.edited}<span>edited</span>{/if}
      <time datetime={message.at}>{time}</time>
      {#if turn}
        <InfoGlyph label={`About the turn that sent this, at ${time}`} class="-my-1 -mr-1.5 size-5">
          <dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 tabular-nums">
            <dt class="opacity-70">Model</dt>
            <dd class="break-all">{turn.model}{turn.fallback ? " (fallback)" : ""}</dd>
            <dt class="opacity-70">Tokens</dt>
            <dd>{tokens(turn.input_tokens)} in, {tokens(turn.cached_tokens)} cached, {tokens(turn.output_tokens)} out</dd>
            <dt class="opacity-70">Cost</dt>
            <dd>{turn.cost_usd === null ? "Not recorded" : cost(turn.cost_usd)}</dd>
            <dt class="opacity-70">Took</dt>
            <dd>{seconds(turn.duration_ms)}{turn.first_bubble_ms !== null ? `, first bubble at ${seconds(turn.first_bubble_ms)}` : ""}</dd>
            {#if turn.lane}<dt class="opacity-70">Lane</dt><dd>{lanes[turn.lane] ?? turn.lane}</dd>{/if}
            <dt class="opacity-70">Calls</dt>
            <dd>{turn.calls}</dd>
          </dl>
        </InfoGlyph>
      {/if}
    </p>
  </div>
</div>
