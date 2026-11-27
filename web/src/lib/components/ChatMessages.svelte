<script lang="ts">
  import { onMount, tick } from "svelte";
  import { getJSON, type ChatMessages } from "$lib/api";
  import MessageBubble from "$lib/components/MessageBubble.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import { Button } from "$lib/components/ui/button";
  import { cn } from "$lib/utils";

  // One chat's recent messages; older pages load as the far end scrolls into view (E85), with "Load older" for
  // keyboards. The owner's DM comes in full, as the Talk page's bubbles: oldest at the top, the newest at the
  // bottom where the box starts, older pages above. Any other chat comes as one line per message, newest first.
  let { chatId, title, turns = true }: { chatId: number; title: string; turns?: boolean } = $props();

  let messages = $state<ChatMessages["messages"]>([]); // newest first, as the API pages them
  let full = $state(false);
  let before = $state<number | null>(null);
  let loaded = $state(false);
  let failed = $state("");
  let loading = $state(false);
  let box = $state<HTMLElement | null>(null);
  let end = $state<HTMLElement | null>(null);

  async function load() {
    if (loading) return;
    loading = true;
    failed = "";
    const first = !loaded;
    // Bubbles grow upwards: keep the ones in view where they are while an older page lands above them.
    const height = box?.scrollHeight ?? 0;
    try {
      const page = await getJSON<ChatMessages>(`/api/chats/${chatId}/messages${before ? `?before=${before}` : ""}`);
      const known = new Set(messages.map((message) => message.id));
      messages = [...messages, ...page.messages.filter((message) => !known.has(message.id))];
      full = page.full;
      before = page.next_before;
      loaded = true;
      if (full) {
        await tick();
        if (box) box.scrollTop = first ? box.scrollHeight : box.scrollTop + box.scrollHeight - height;
        if (first) settleUntil = Date.now() + 5000;
      }
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    } finally {
      loading = false;
    }
  }
  onMount(load);

  // Photos and videos load after the first scroll to the newest message and push it up: for a few seconds, each one
  // that arrives scrolls back down, unless the owner has scrolled away.
  let settleUntil = 0;
  function settle() {
    if (!box || Date.now() > settleUntil) return;
    if (box.scrollHeight - box.scrollTop - box.clientHeight < 400) box.scrollTop = box.scrollHeight;
  }

  // Progressive loading: the far end of the list coming into view (inside its own scroll box) asks for the next page.
  $effect(() => {
    if (!end || !box || before === null || failed || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => entries.some((entry) => entry.isIntersecting) && load(), { root: box, rootMargin: "120px" });
    observer.observe(end);
    return () => observer.disconnect();
  });

  const oldestFirst = $derived([...messages].reverse());
  const day = (iso: string) => new Date(iso).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  const time = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
</script>

{#snippet older()}
  <div bind:this={end} class={full ? "pb-2" : "pt-2"}>
    <Button variant="ghost" size="sm" class="w-full" disabled={loading} onclick={load}>
      {loading ? "Loading…" : failed ? "Couldn't load older messages. Try again" : "Load older messages"}
    </Button>
  </div>
{/snippet}

<section aria-label={`Recent messages in ${title}`} class="grid gap-3 border-t pt-4">
  <div class="flex flex-wrap items-center justify-between gap-2">
    <p class="flex items-center gap-1 text-sm font-medium">
      Recent messages
      {#if loaded && !full}<InfoGlyph label="About these messages">One line per message: the dashboard shows only your own DM in full.</InfoGlyph>{/if}
      {#if loaded && full}<InfoGlyph label="About these messages">The same conversation as Talk, newest at the bottom. The (i) on Dom's messages shows the turn that sent them: model, tokens, cost, time and lane.</InfoGlyph>{/if}
    </p>
    {#if turns}
      <a href={`/app/system/turns?chat=${chatId}`} class="text-sm font-medium text-signal-strong underline-offset-4 hover:underline">Turn log for this chat</a>
    {/if}
  </div>
  {#if !loaded && failed}
    <p role="alert" class="text-sm text-coral">Couldn't load the messages: {failed}. <Button variant="link" size="xs" onclick={load}>Retry</Button></p>
  {:else if !loaded}
    <div class="h-24 animate-pulse rounded-lg bg-raised/60" aria-hidden="true"></div>
    <p class="sr-only" role="status">Loading messages…</p>
  {:else if messages.length === 0}
    <p class="text-sm text-muted-foreground">No messages yet.</p>
  {:else if full}
    <!-- It scrolls by itself, so it takes keyboard focus to scroll from the keyboard. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div
      bind:this={box}
      onloadcapture={settle}
      onloadedmetadatacapture={settle}
      role="region"
      aria-label={`Messages in ${title}, oldest first`}
      tabindex="0"
      class="relative max-h-[28rem] overflow-y-auto overscroll-contain rounded-lg bg-canvas/40 px-2 py-3 outline-none focus-visible:ring-3 focus-visible:ring-ring/50 sm:px-3"
    >
      {#if before !== null}{@render older()}{/if}
      <ol class="grid grid-cols-[minmax(0,1fr)] gap-1.5">
        {#each oldestFirst as message, index (message.id)}
          {@const iso = message.iso ?? ""}
          {#if iso && (index === 0 || day(iso) !== day(oldestFirst[index - 1].iso ?? ""))}
            <li class="my-2 text-center text-caption text-muted-foreground" aria-hidden="true">{day(iso)}</li>
          {/if}
          <li>
            <MessageBubble
              message={{ ...message, at: iso, origin: message.origin ?? null, edited: message.edited ?? false, media: message.media ?? null }}
              time={iso ? time(iso) : message.at}
              turn={message.turn ?? null}
            />
          </li>
        {/each}
      </ol>
    </div>
  {:else}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div bind:this={box} role="region" aria-label={`Messages in ${title}, newest first`} tabindex="0" class="max-h-96 overflow-y-auto rounded-lg outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
      <ol class="grid gap-2">
        {#each messages as message (message.id)}
          <li class={cn("min-w-0 rounded-lg border px-3 py-2 text-sm", message.mine ? "border-signal/30 bg-tint" : "bg-raised/40")}>
            <p class="flex justify-between gap-3 text-xs text-muted-foreground">
              <span class="font-medium text-foreground">{message.who}</span><span class="tabular-nums">{message.at}</span>
            </p>
            <p class="mt-0.5 truncate break-words [overflow-wrap:anywhere]">{message.text}</p>
          </li>
        {/each}
      </ol>
      {#if before !== null}{@render older()}{/if}
    </div>
  {/if}
</section>
