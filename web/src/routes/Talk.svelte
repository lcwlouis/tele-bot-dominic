<script lang="ts">
  import ChevronDown from "@lucide/svelte/icons/chevron-down";
  import ImagePlus from "@lucide/svelte/icons/image-plus";
  import Monitor from "@lucide/svelte/icons/monitor";
  import PanelRight from "@lucide/svelte/icons/panel-right";
  import SendIcon from "@lucide/svelte/icons/send";
  import X from "@lucide/svelte/icons/x";
  import { onMount, tick } from "svelte";
  import { getJSON, send, type HomeData, type TalkData, type TalkMessage, type TraceSummary } from "$lib/api";
  import { ACTIVITY_LABELS } from "$lib/components/Orbit.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import TraceLoader from "$lib/components/trace/TraceLoader.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button, buttonVariants } from "$lib/components/ui/button";
  import { Label } from "$lib/components/ui/label";
  import * as Sheet from "$lib/components/ui/sheet";
  import { Switch } from "$lib/components/ui/switch";
  import { cn } from "$lib/utils";

  // The owner DM, written to from the dashboard (dom/talk.py): the same conversation as Telegram. The page polls
  // /api/talk with a cursor, every second while Dom is composing and every few seconds otherwise, and not at all
  // while the tab is hidden. The Trace switch lays each turn's full audit (dom/tracer.py) between the messages.
  const FAST_MS = 1000;
  const SLOW_MS = 4000;
  const PANEL_MS = 15_000;

  const params = new URLSearchParams(location.search);
  let tracing = $state(params.get("trace") === "1");
  let openTurn = $state<number | null>(params.get("turn") ? Number(params.get("turn")) : null);

  let messages = $state<TalkMessage[]>([]);
  let turns = $state<Map<number, TraceSummary>>(new Map());
  let hasMore = $state(false);
  let typing = $state(false);
  let traced = $state(true);
  let loaded = $state(false);
  let failed = $state("");
  let home = $state<HomeData | null>(null);
  let panelOpen = $state(false);

  let draft = $state("");
  let photo = $state<{ name: string; data: string; mime: string } | null>(null);
  let sending = $state(false);
  let scroller = $state<HTMLElement | null>(null);
  let fileInput = $state<HTMLInputElement | null>(null);

  const lastId = () => (messages.length ? messages[messages.length - 1].id : 0);

  function merge(rows: TalkMessage[], older = false) {
    const known = new Set(messages.map((row) => row.id));
    const fresh = rows.filter((row) => !known.has(row.id));
    messages = older ? [...fresh, ...messages] : [...messages, ...fresh];
    return fresh.length;
  }
  function mergeTurns(rows: TraceSummary[] | undefined) {
    if (!rows?.length) return;
    const next = new Map(turns);
    for (const row of rows) next.set(row.id, row);
    turns = next;
  }

  const nearBottom = () => !scroller || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120;
  async function toBottom(force = false) {
    if (!force && !nearBottom()) return;
    await tick();
    scroller?.scrollTo({ top: scroller.scrollHeight });
  }

  async function load() {
    try {
      const data = await getJSON<TalkData>(`/api/talk${tracing ? "?trace=1" : ""}`);
      messages = [];
      merge(data.messages);
      turns = new Map();
      mergeTurns(data.turns);
      ({ has_more: hasMore, typing, traced } = data);
      failed = "";
      loaded = true;
      await toBottom(true);
      if (openTurn !== null) document.getElementById(`turn-${openTurn}`)?.scrollIntoView({ block: "start" });
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  async function poll() {
    if (!loaded || document.visibilityState !== "visible") return;
    try {
      const stick = nearBottom();
      const data = await getJSON<TalkData>(`/api/talk?after=${lastId()}${tracing ? "&trace=1" : ""}`);
      const added = merge(data.messages);
      mergeTurns(data.turns);
      typing = data.typing;
      if ((added || data.turns?.length) && stick) await toBottom(true);
    } catch {
      // a missed poll is retried on the next one; the page keeps what it has
    }
  }

  async function older() {
    const first = messages[0]?.id;
    if (!first) return;
    const height = scroller?.scrollHeight ?? 0;
    const data = await getJSON<TalkData>(`/api/talk?before=${first}${tracing ? "&trace=1" : ""}`);
    merge(data.messages, true);
    mergeTurns(data.turns);
    hasMore = data.has_more;
    await tick();
    if (scroller) scroller.scrollTop += scroller.scrollHeight - height;
  }

  async function loadHome() {
    try {
      home = await getJSON<HomeData>("/api/home");
    } catch {
      // the side panel is a convenience: it keeps the last answer
    }
  }

  onMount(() => {
    load();
    loadHome();
    let timer: ReturnType<typeof setTimeout>;
    const loop = async () => {
      await poll();
      timer = setTimeout(loop, typing ? FAST_MS : SLOW_MS);
    };
    timer = setTimeout(loop, SLOW_MS);
    const panel = setInterval(() => document.visibilityState === "visible" && loadHome(), PANEL_MS);
    const onVisible = () => document.visibilityState === "visible" && poll();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearTimeout(timer);
      clearInterval(panel);
      document.removeEventListener("visibilitychange", onVisible);
    };
  });

  function setTracing(value: boolean) {
    tracing = value;
    const url = new URL(location.href);
    if (value) url.searchParams.set("trace", "1");
    else {
      url.searchParams.delete("trace");
      url.searchParams.delete("turn");
      openTurn = null;
    }
    history.replaceState(history.state, "", url);
    load();
  }

  async function submit() {
    const text = draft.trim();
    if ((!text && !photo) || sending) return;
    sending = true;
    try {
      await send("/api/talk/send", photo ? { text, photo: photo.data, mime: photo.mime } : { text });
      draft = "";
      clearPhoto();
      typing = true;
      await poll();
      await toBottom(true);
    } catch {
      // send() showed the server's reason; the draft stays so nothing is lost
    } finally {
      sending = false;
    }
  }

  function onKey(event: KeyboardEvent) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  function pick(event: Event) {
    const file = (event.currentTarget as HTMLInputElement).files?.[0];
    if (!file) return;
    if (file.size > 8_000_000) {
      failedPhoto = "That photo is over 8 MB.";
      return;
    }
    failedPhoto = "";
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result);
      photo = { name: file.name, data: url.slice(url.indexOf(",") + 1), mime: file.type };
    };
    reader.readAsDataURL(file);
  }
  let failedPhoto = $state("");
  function clearPhoto() {
    photo = null;
    if (fileInput) fileInput.value = "";
  }

  // The conversation and, with Trace on, each turn placed where it started: after the messages it answered and
  // before the bubbles it sent. Work after a turn sits inside that turn's audit; background work stands alone.
  type Item =
    | { kind: "message"; at: string; key: string; message: TalkMessage }
    | { kind: "turn"; at: string; key: string; turn: TraceSummary };
  const items = $derived.by<Item[]>(() => {
    const list: Item[] = messages.map((message) => ({ kind: "message", at: message.at, key: `m${message.id}`, message }));
    if (tracing) {
      const first = messages[0]?.at ?? "";
      for (const turn of turns.values()) {
        if (turn.kind === "after" || turn.at < first) continue;
        list.push({ kind: "turn", at: turn.at, key: `t${turn.id}`, turn });
      }
    }
    return list.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : a.kind === "message" ? -1 : 1));
  });

  const day = (iso: string) => new Date(iso).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  const time = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  const newDay = (index: number) => index === 0 || day(items[index].at) !== day(items[index - 1].at);
  const stateWord = { online: "Online", asleep: "Asleep", offline: "Away" } as const;
</script>

{#snippet panel()}
  {#if home && !home.setup}
    <div class="grid gap-5">
      <section aria-labelledby="talk-profile">
        <h2 id="talk-profile" class="text-meta font-semibold text-muted-foreground">Dom</h2>
        <p class="mt-1.5 flex items-center gap-2 font-medium">
          <span class="size-2 rounded-full {home.presence.state === 'online' ? 'bg-status-done' : home.presence.state === 'asleep' ? 'bg-status-paused' : 'bg-muted-foreground'}" aria-hidden="true"></span>
          {stateWord[home.presence.state]}
        </p>
        <p class="mt-0.5 text-meta text-muted-foreground">{home.presence.detail}</p>
        <p class="mt-1.5 text-meta">{ACTIVITY_LABELS[home.activity]}</p>
        {#if home.vibe.mode !== "none"}<p class="mt-1.5 text-meta text-muted-foreground">Vibe: {home.vibe.title}</p>{/if}
      </section>
      <section aria-labelledby="talk-waiting">
        <h2 id="talk-waiting" class="text-meta font-semibold text-muted-foreground">Waiting for you <span class="font-normal">{home.waiting.count}</span></h2>
        {#if home.waiting.items.length}
          <ul class="mt-1.5 grid gap-1.5 text-sm">
            {#each home.waiting.items.slice(0, 5) as item, index (index)}
              <li><span class="font-medium">{item.label}</span> <span class="text-muted-foreground">{item.detail}</span></li>
            {/each}
          </ul>
          <a href={home.waiting.href} class="mt-2 inline-block text-meta text-signal-strong underline-offset-4 hover:underline">Open the approvals</a>
        {:else}
          <p class="mt-1.5 text-meta text-muted-foreground">Nothing needs your yes or no.</p>
        {/if}
      </section>
      <section aria-labelledby="talk-work">
        <h2 id="talk-work" class="text-meta font-semibold text-muted-foreground">Working on</h2>
        {#if home.desktop.task}
          <p class="mt-1.5 text-sm"><Monitor class="mr-1 inline size-3.5 align-[-2px]" aria-hidden="true" />{home.desktop.task.brief}</p>
          <p class="text-meta text-muted-foreground">{home.desktop.task.status}</p>
        {:else}
          <p class="mt-1.5 text-meta text-muted-foreground">No desktop task running.</p>
        {/if}
        <a href="/app/work" class="mt-2 inline-block text-meta text-signal-strong underline-offset-4 hover:underline">Tasks and own work</a>
      </section>
    </div>
  {:else}
    <div class="h-40 animate-pulse rounded-lg bg-raised" aria-hidden="true"></div>
  {/if}
{/snippet}

<PageHeader kicker="Talk" title="Dom" subtitle="The same conversation as your Telegram DM. What you write here goes there too.">
  {#snippet aside()}
    <div class="flex items-center gap-2">
      <Switch id="talk-trace" checked={tracing} onCheckedChange={setTracing} disabled={!traced} />
      <Label for="talk-trace">Trace</Label>
      <InfoGlyph label="About Trace">Shows every turn's full audit between the messages: triage, the exact prompt, each model call with its request and response, tools, checks, delivery and what ran after. Secrets are redacted.</InfoGlyph>
    </div>
    <Sheet.Root bind:open={panelOpen}>
      <Sheet.Trigger class={cn(buttonVariants({ variant: "outline", size: "sm" }), "lg:hidden")}>
        <PanelRight aria-hidden="true" />Dom right now
      </Sheet.Trigger>
      <Sheet.Content onswipe={() => (panelOpen = false)}>
        <div class="flex items-center justify-between px-4 pt-4 pb-2">
          <Sheet.Title class="text-lg font-semibold tracking-tight">Dom right now</Sheet.Title>
          <Sheet.Close class={buttonVariants({ variant: "ghost", size: "sm" })}>Done</Sheet.Close>
        </div>
        <div class="px-4 pb-6">{@render panel()}</div>
      </Sheet.Content>
    </Sheet.Root>
  {/snippet}
</PageHeader>

{#if !loaded && failed}
  <StatusNote tone="error" title="Couldn't load the conversation" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="outline" onclick={load}>Try again</Button>
  </StatusNote>
{:else}
  <div class="grid gap-6 lg:grid-cols-[minmax(0,1fr)_17rem]">
    <div class="flex min-w-0 flex-col rounded-xl border bg-surface">
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <div bind:this={scroller} tabindex="0" role="log" aria-label="Conversation with Dom" aria-live="polite" class="h-[62dvh] overflow-y-auto overscroll-contain px-3 py-4 sm:px-5 lg:h-[calc(100dvh-19rem)]">
        {#if !loaded}
          <div class="grid gap-3" aria-hidden="true">
            {#each [0, 1, 2] as index (index)}<div class="h-12 w-2/3 animate-pulse rounded-2xl bg-raised {index % 2 ? 'ml-auto' : ''}"></div>{/each}
          </div>
        {:else}
          {#if hasMore}
            <div class="mb-4 text-center"><Button variant="outline" size="sm" onclick={older}>Load earlier messages</Button></div>
          {/if}
          {#if !items.length}
            <StatusNote title="Nothing here yet" detail="Say hi: Dom answers here and in Telegram." />
          {/if}
          <ol class="grid grid-cols-[minmax(0,1fr)] gap-1.5" aria-label="Messages and turns">
            {#each items as item, index (item.key)}
              {#if newDay(index)}
                <li class="my-3 text-center text-caption text-muted-foreground" aria-hidden="true">{day(item.at)}</li>
              {/if}
              {#if item.kind === "message"}
                {@const message = item.message}
                <li class="flex {message.mine ? 'justify-start' : 'justify-end'}">
                  <div class="bubble max-w-[85%] rounded-2xl px-3.5 py-2 sm:max-w-[70%] {message.mine ? 'rounded-bl-md bg-raised' : 'rounded-br-md bg-tint'}">
                    <span class="sr-only">{message.who}{message.origin === "dashboard" ? ", from the dashboard" : ""}:</span>
                    {#if message.media?.url && message.media.kind === "photo"}
                      <img src={message.media.url} alt={message.media.description ?? "A photo Dom sent"} class="mb-1.5 max-h-72 rounded-lg" loading="lazy" />
                    {:else if message.media?.url && message.media.kind === "voice"}
                      <audio controls src={message.media.url} class="mb-1.5 max-w-full" aria-label="Voice note from Dom"></audio>
                    {:else if message.media?.url}
                      <!-- svelte-ignore a11y_media_has_caption -->
                      <video controls src={message.media.url} class="mb-1.5 max-h-72 rounded-full" aria-label="Round video from Dom"></video>
                    {:else if message.media}
                      <Badge variant="outline" class="mb-1">{message.media.kind}{message.media.description ? `: ${message.media.description}` : ""}</Badge>
                    {/if}
                    {#if message.text && !(message.media && message.text === `[${message.media.kind}]`)}
                      <p class="text-body break-words whitespace-pre-wrap">{message.text}</p>
                    {/if}
                    <p class="mt-0.5 flex items-center justify-end gap-1 text-caption text-muted-foreground">
                      {#if message.origin === "dashboard"}<span title="Sent from the dashboard" aria-hidden="true">🖥</span>{/if}
                      {#if message.edited}<span>edited</span>{/if}
                      <time datetime={message.at}>{time(message.at)}</time>
                    </p>
                  </div>
                </li>
              {:else}
                {@const turn = item.turn}
                <li id={`turn-${turn.turn_id ?? turn.id}`} class="my-1.5 min-w-0">
                  <details
                    class="group/turn rounded-xl border border-dashed bg-canvas/60"
                    open={openTurn !== null && openTurn === turn.turn_id}
                    ontoggle={(event) => {
                      if ((event.currentTarget as HTMLDetailsElement).open && turn.turn_id !== null) openTurn = turn.turn_id;
                    }}
                  >
                    <summary class="flex min-h-10 cursor-pointer list-none flex-wrap items-center gap-x-2 rounded-xl px-3 text-meta outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
                      <ChevronDown class="size-3.5 shrink-0 text-muted-foreground transition-transform duration-(--motion-quick) group-open/turn:rotate-180" aria-hidden="true" />
                      <span class="font-medium">{turn.kind === "turn" ? "Turn" : turn.label}</span>
                      <span class="text-muted-foreground">{turn.outcome || "Stayed quiet"} · {time(turn.at)}</span>
                    </summary>
                    <div class="border-t px-3 py-3">
                      {#if turn.kind === "turn" && turn.turn_id !== null}
                        <TraceLoader turnId={turn.turn_id} />
                      {:else}
                        <TraceLoader partId={turn.id} />
                      {/if}
                    </div>
                  </details>
                </li>
              {/if}
            {/each}
            {#if typing}
              <li class="flex justify-start" aria-label="Dom is typing">
                <div class="flex items-center gap-1 rounded-2xl rounded-bl-md bg-raised px-3.5 py-3" aria-hidden="true">
                  <span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>
                </div>
              </li>
            {/if}
          </ol>
        {/if}
      </div>

      <form
        class="border-t p-2.5 sm:p-3"
        onsubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        {#if photo}
          <div class="mb-2 flex items-center gap-2">
            <!-- No thumbnail: Dom's CSP allows images from its own address only, not blob: or data: URLs. -->
            <ImagePlus class="size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span class="min-w-0 flex-1 truncate text-meta text-muted-foreground">Attached {photo.name}</span>
            <IconButton label="Remove the photo" icon={X} onclick={clearPhoto} />
          </div>
        {/if}
        {#if failedPhoto}<p class="mb-2 text-meta text-status-failed" role="alert">{failedPhoto}</p>{/if}
        <div class="flex items-end gap-2">
          <input bind:this={fileInput} type="file" accept="image/jpeg,image/png,image/webp" class="sr-only" id="talk-photo" tabindex="-1" aria-label="Photo to send" onchange={pick} />
          <IconButton label="Attach a photo" icon={ImagePlus} onclick={() => fileInput?.click()} variant="ghost" size="icon" />
          <label for="talk-draft" class="sr-only">Message to Dom</label>
          <textarea
            id="talk-draft"
            bind:value={draft}
            onkeydown={onKey}
            rows="1"
            maxlength="4000"
            placeholder="Message Dom"
            class="field-sizing-content max-h-40 min-h-10 min-w-0 flex-1 resize-none rounded-lg border bg-raised px-3 py-2 text-body outline-none placeholder:text-muted-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
          ></textarea>
          <IconButton label={sending ? "Sending…" : "Send"} shortcut="Enter" icon={SendIcon} type="submit" disabled={sending || (!draft.trim() && !photo)} variant="default" size="icon" />
        </div>
      </form>
    </div>

    <aside class="hidden lg:block" aria-label="Dom right now">
      <div class="sticky top-4 rounded-xl border bg-surface p-4">{@render panel()}</div>
    </aside>
  </div>
{/if}

<style>
  .typing-dot {
    width: 0.4rem;
    height: 0.4rem;
    border-radius: 9999px;
    background: var(--color-muted-foreground);
    animation: typing 1.2s infinite ease-in-out;
  }
  .typing-dot:nth-child(2) {
    animation-delay: 0.15s;
  }
  .typing-dot:nth-child(3) {
    animation-delay: 0.3s;
  }
  @keyframes typing {
    0%,
    80%,
    100% {
      opacity: 0.3;
    }
    40% {
      opacity: 1;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .typing-dot {
      animation: none;
      opacity: 0.6;
    }
  }
</style>
