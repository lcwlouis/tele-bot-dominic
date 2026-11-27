<script lang="ts">
  import ChevronDown from "@lucide/svelte/icons/chevron-down";
  import ImagePlus from "@lucide/svelte/icons/image-plus";
  import Paperclip from "@lucide/svelte/icons/paperclip";
  import Monitor from "@lucide/svelte/icons/monitor";
  import PanelRight from "@lucide/svelte/icons/panel-right";
  import SendIcon from "@lucide/svelte/icons/send";
  import X from "@lucide/svelte/icons/x";
  import { onMount, tick, untrack } from "svelte";
  import { MediaQuery } from "svelte/reactivity";
  import { getJSON, send, type HomeData, type TalkData, type TalkMessage, type TraceSummary } from "$lib/api";
  import { ACTIVITY_LABELS } from "$lib/components/Orbit.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import MessageBubble from "$lib/components/MessageBubble.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import TraceLoader from "$lib/components/trace/TraceLoader.svelte";
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
  // One attachment at a time: a photo (Dom sees it as a photo) or any file, which goes to Telegram as a document and
  // lands in the workspace as one sent there does. The limits are the server's (dom/talk.py).
  type Attachment = { kind: "photo" | "file"; name: string; data: string; mime: string };
  const LIMITS = { photo: 8_000_000, file: 20_000_000 } as const;
  let attachment = $state<Attachment | null>(null);
  let sending = $state(false);
  let scroller = $state<HTMLElement | null>(null);
  let photoInput = $state<HTMLInputElement | null>(null);
  let fileInput = $state<HTMLInputElement | null>(null);

  const lastId = () => (messages.length ? messages[messages.length - 1].id : 0);
  // Traces have their own cursor: a long turn, or a slow writer, finishes after the messages it answered.
  const lastTrace = () => Math.max(0, ...turns.keys());
  // The ids of every stored part of one turn (the turn, then its after parts as they arrive).
  const partsOf = (turnId: number) =>
    [...turns.values()].filter((part) => part.turn_id === turnId).map((part) => part.id).sort((a, b) => a - b).join(",");
  // Every request this page makes stops when it goes away, so no poll outlives it.
  const abort = new AbortController();
  let disposed = false;

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

  // Photos and videos load after the first scroll to the bottom and push it up: for a few seconds after a load, each
  // one that arrives scrolls back down (unless a turn was asked for, or the owner scrolled away).
  let settleUntil = 0;
  function settle() {
    if (Date.now() < settleUntil && openTurn === null && nearBottom(400)) scroller?.scrollTo({ top: scroller.scrollHeight });
  }
  const nearBottom = (within = 120) => !scroller || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < within;
  async function toBottom(force = false) {
    if (!force && !nearBottom()) return;
    await tick();
    scroller?.scrollTo({ top: scroller.scrollHeight });
  }

  async function load() {
    try {
      const data = await getJSON<TalkData>(`/api/talk${tracing ? "?trace=1" : ""}`, abort.signal);
      messages = [];
      merge(data.messages);
      turns = new Map();
      mergeTurns(data.turns);
      ({ has_more: hasMore, typing, traced } = data);
      failed = "";
      loaded = true;
      settleUntil = Date.now() + 5000;
      await toBottom(true);
      if (openTurn !== null) document.getElementById(`turn-${openTurn}`)?.scrollIntoView({ block: "start" });
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  async function poll() {
    if (disposed || !loaded || document.visibilityState !== "visible") return;
    try {
      const stick = nearBottom();
      const data = await getJSON<TalkData>(`/api/talk?after=${lastId()}${tracing ? `&trace=1&trace_after=${lastTrace()}` : ""}`, abort.signal);
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
    const data = await getJSON<TalkData>(`/api/talk?before=${first}${tracing ? "&trace=1" : ""}`, abort.signal);
    merge(data.messages, true);
    mergeTurns(data.turns);
    hasMore = data.has_more;
    await tick();
    if (scroller) scroller.scrollTop += scroller.scrollHeight - height;
  }

  async function loadHome() {
    try {
      home = await getJSON<HomeData>("/api/home", abort.signal);
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
      if (!disposed) timer = setTimeout(loop, typing ? FAST_MS : SLOW_MS);
    };
    timer = setTimeout(loop, SLOW_MS);
    const panel = setInterval(() => document.visibilityState === "visible" && loadHome(), PANEL_MS);
    const onVisible = () => document.visibilityState === "visible" && poll();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      disposed = true;
      abort.abort();
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
    if ((!text && !attachment) || sending || reading) return;
    sending = true;
    try {
      const body =
        attachment?.kind === "photo"
          ? { text, photo: attachment.data, mime: attachment.mime }
          : attachment
            ? { text, file: attachment.data, name: attachment.name }
            : { text };
      await send("/api/talk/send", body);
      draft = "";
      clearAttachment();
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

  function pick(kind: Attachment["kind"], event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = ""; // the same file can be chosen again after it is removed
    if (!file) return;
    // Any pick replaces what was attached or still being read, even one that is then refused for its size.
    clearAttachment();
    if (file.size > LIMITS[kind]) {
      failedAttach = `That ${kind} is over ${LIMITS[kind] / 1_000_000} MB.`;
      return;
    }
    if (!file.size) {
      failedAttach = "That file is empty.";
      return;
    }
    failedAttach = "";
    // Nothing is attached until this file is read, and a read that finishes after another pick (or a remove) is
    // dropped, so Send can never carry the previous file.
    const pick = ++picks;
    reading = true;
    const reader = new FileReader();
    reader.onload = () => {
      if (pick !== picks) return;
      const url = String(reader.result);
      attachment = { kind, name: file.name, data: url.slice(url.indexOf(",") + 1), mime: file.type };
      reading = false;
    };
    reader.onerror = () => {
      if (pick !== picks) return;
      failedAttach = `Couldn't read that ${kind}. Choose it again.`;
      reading = false;
    };
    reader.readAsDataURL(file);
  }
  let failedAttach = $state("");
  let reading = $state(false);
  let picks = 0;
  function clearAttachment() {
    picks += 1;
    attachment = null;
    reading = false;
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

  // Phone: the shell is the visible screen (App.svelte), kept here on the visual viewport, which shrinks while the
  // keyboard is up and, on iOS, pans under it. At rest the card fills what is left below the header; while writing it
  // is the whole visible height and the shell scrolls so its bottom (the composer) sits on the keyboard. A wide screen
  // sizes the card with CSS.
  const wide = new MediaQuery("(min-width: 1024px)");
  let card = $state<HTMLElement | null>(null);
  let composing = $state(false);
  let visible = $state(0);
  const cardHeight = $derived(!wide.current && composing && visible ? `${Math.round(visible - 8)}px` : undefined);
  function fit() {
    if (card && !wide.current) card.scrollIntoView({ block: "end", behavior: "instant" });
  }
  // A tap elsewhere blurs the draft on its way down: the card keeps its size until the tap is over, so what was tapped
  // (Trace, Dom right now) doesn't move out from under it.
  let pressing = false;
  function stopComposing() {
    if (!pressing) {
      composing = false;
      return;
    }
    const done = () => {
      window.removeEventListener("pointerup", done);
      window.removeEventListener("pointercancel", done);
      setTimeout(() => (composing = document.activeElement?.id === "talk-draft"));
    };
    window.addEventListener("pointerup", done);
    window.addEventListener("pointercancel", done);
  }
  async function startComposing() {
    composing = true;
    if (wide.current) return;
    await tick();
    fit();
    await toBottom(true);
  }
  $effect(() => {
    const view = window.visualViewport;
    const style = document.documentElement.style;
    const measure = async () => {
      if (view && Math.abs(view.scale - 1) > 0.01) return; // a pinch zoom shrinks it too: follow the keyboard, not that
      visible = view?.height ?? window.innerHeight;
      style.setProperty("--visible-height", `${visible}px`);
      style.setProperty("--visible-top", `${view?.offsetTop ?? 0}px`);
      if (composing) {
        await tick();
        fit();
      }
    };
    const press = (event: PointerEvent) => (pressing = event.type === "pointerdown");
    untrack(measure);
    window.addEventListener("pointerdown", press, true);
    window.addEventListener("pointerup", press, true);
    window.addEventListener("pointercancel", press, true);
    view?.addEventListener("resize", measure);
    view?.addEventListener("scroll", measure);
    window.addEventListener("resize", measure);
    return () => {
      view?.removeEventListener("resize", measure);
      view?.removeEventListener("scroll", measure);
      window.removeEventListener("resize", measure);
      window.removeEventListener("pointerdown", press, true);
      window.removeEventListener("pointerup", press, true);
      window.removeEventListener("pointercancel", press, true);
      style.removeProperty("--visible-height");
      style.removeProperty("--visible-top");
    };
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

<PageHeader kicker="Talk" title="Dom" subtitle="The same conversation as your Telegram DM. What you write here goes there too." />

{#if !loaded && failed}
  <StatusNote tone="error" title="Couldn't load the conversation" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="outline" onclick={load}>Try again</Button>
  </StatusNote>
{:else}
  <div class="grid gap-6 max-lg:min-h-0 max-lg:flex-1 max-lg:grid-rows-[minmax(0,1fr)] lg:grid-cols-[minmax(0,1fr)_17rem]">
    <!-- On a phone the card fills the screen below the header, and the whole visible height above the keyboard while
         writing, so the composer sits on the keyboard and the conversation above it. -->
    <div bind:this={card} style:height={cardHeight} class="relative flex min-w-0 flex-col overflow-hidden rounded-xl border bg-surface">
      <!-- The card's own bar keeps Trace (and, on a phone, Dom right now) in reach while the page is scrolled to it. -->
      <div class="flex min-h-11 items-center gap-2 border-b px-3 py-1.5 sm:px-4">
        <Switch id="talk-trace" checked={tracing} onCheckedChange={setTracing} disabled={!traced} />
        <Label for="talk-trace">Trace</Label>
        <InfoGlyph label="About Trace">Shows every turn's full audit between the messages: triage, the exact prompt, each model call with its request and response, tools, checks, delivery and what ran after. Secrets are redacted.</InfoGlyph>
        <Sheet.Root bind:open={panelOpen}>
          <Sheet.Trigger class={cn(buttonVariants({ variant: "ghost", size: "sm" }), "ml-auto lg:hidden")}>
            <PanelRight aria-hidden="true" />Dom right now
          </Sheet.Trigger>
          <Sheet.Content onswipe={() => (panelOpen = false)}>
            <div class="flex items-center justify-between px-4 pt-4 pb-2">
              <Sheet.Title class="text-lg font-semibold tracking-tight">Dom right now</Sheet.Title>
              <Sheet.Close class={buttonVariants({ variant: "ghost", size: "sm" })}>Done</Sheet.Close>
            </div>
            <div class="overflow-y-auto px-4 pb-[max(1.5rem,env(safe-area-inset-bottom))]">{@render panel()}</div>
          </Sheet.Content>
        </Sheet.Root>
      </div>
      <!-- Positioned, so what is placed inside it (screen-reader text) scrolls with it and never stretches the page. -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <div bind:this={scroller} onloadcapture={settle} onloadedmetadatacapture={settle} tabindex="0" role="log" aria-label="Conversation with Dom" aria-live="polite" class="relative min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 py-4 sm:px-5 lg:h-[calc(100dvh-22rem)] lg:flex-none">
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
                <li><MessageBubble {message} time={time(message.at)} /></li>
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
                        <!-- Keyed on the turn's parts, so work after the turn that arrives later loads in. -->
                        {#key partsOf(turn.turn_id)}<TraceLoader turnId={turn.turn_id} />{/key}
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
        class={cn("border-t p-2.5 sm:p-3", !composing && "pb-[max(0.625rem,env(safe-area-inset-bottom))]")}
        onsubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        {#if attachment}
          {@const Icon = attachment.kind === "photo" ? ImagePlus : Paperclip}
          <div class="mb-2 flex items-center gap-2">
            <!-- No thumbnail: Dom's CSP allows images from its own address only, not blob: or data: URLs. -->
            <Icon class="size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span class="min-w-0 flex-1 truncate text-meta text-muted-foreground">Attached {attachment.name}</span>
            <IconButton label={`Remove the ${attachment.kind}`} icon={X} onclick={clearAttachment} />
          </div>
        {/if}
        {#if failedAttach}<p class="mb-2 text-meta text-status-failed" role="alert">{failedAttach}</p>{/if}
        <div class="flex items-end gap-1 sm:gap-2">
          <input bind:this={photoInput} type="file" accept="image/jpeg,image/png,image/webp" class="sr-only" id="talk-photo" tabindex="-1" aria-label="Photo to send" onchange={(event) => pick("photo", event)} />
          <input bind:this={fileInput} type="file" class="sr-only" id="talk-file" tabindex="-1" aria-label="File to send" onchange={(event) => pick("file", event)} />
          <IconButton label="Attach a photo" icon={ImagePlus} onclick={() => photoInput?.click()} variant="ghost" size="icon" />
          <IconButton label="Attach a file" icon={Paperclip} onclick={() => fileInput?.click()} variant="ghost" size="icon" />
          <label for="talk-draft" class="sr-only">Message to Dom</label>
          <textarea
            id="talk-draft"
            bind:value={draft}
            onkeydown={onKey}
            onfocus={startComposing}
            onblur={stopComposing}
            rows="1"
            maxlength="4000"
            placeholder="Message Dom"
            class="field-sizing-content max-h-40 min-h-10 min-w-0 flex-1 resize-none rounded-lg border bg-raised px-3 py-2 text-body outline-none placeholder:text-muted-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
          ></textarea>
          <!-- Pressing Send keeps the draft's focus, so a phone's keyboard stays up and the card doesn't resize under the tap. -->
          <IconButton label={sending ? "Sending…" : "Send"} shortcut="Enter" icon={SendIcon} type="submit" onmousedown={(event: MouseEvent) => event.preventDefault()} disabled={sending || reading || (!draft.trim() && !attachment)} variant="default" size="icon" />
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
