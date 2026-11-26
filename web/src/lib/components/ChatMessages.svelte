<script lang="ts">
  import { onMount } from "svelte";
  import { getJSON, type ChatMessages } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import { Button } from "$lib/components/ui/button";
  import { cn } from "$lib/utils";

  // One chat's recent messages, newest first; older pages load as the end scrolls into view (E85), with "Load
  // older" for keyboards. The owner's DM comes in full; any other chat comes as one line per message.
  let { chatId, title, turns = true }: { chatId: number; title: string; turns?: boolean } = $props();

  let messages = $state<ChatMessages["messages"]>([]);
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
    try {
      const page = await getJSON<ChatMessages>(`/api/chats/${chatId}/messages${before ? `?before=${before}` : ""}`);
      const known = new Set(messages.map((message) => message.id));
      messages = [...messages, ...page.messages.filter((message) => !known.has(message.id))];
      full = page.full;
      before = page.next_before;
      loaded = true;
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    } finally {
      loading = false;
    }
  }
  onMount(load);

  // Progressive loading: the end of the list coming into view (inside its own scroll box) asks for the next page.
  $effect(() => {
    if (!end || !box || before === null || failed || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => entries.some((entry) => entry.isIntersecting) && load(), { root: box, rootMargin: "120px" });
    observer.observe(end);
    return () => observer.disconnect();
  });
</script>

<section aria-label={`Recent messages in ${title}`} class="grid gap-3 border-t pt-4">
  <div class="flex flex-wrap items-center justify-between gap-2">
    <p class="flex items-center gap-1 text-sm font-medium">
      Recent messages
      {#if loaded && !full}<InfoGlyph label="About these messages">One line per message: the dashboard shows only your own DM in full.</InfoGlyph>{/if}
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
  {:else}
    <!-- It scrolls by itself, so it takes keyboard focus to scroll from the keyboard. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div bind:this={box} role="region" aria-label={`Messages in ${title}, newest first`} tabindex="0" class="max-h-96 overflow-y-auto rounded-lg outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
      <ol class="grid gap-2">
        {#each messages as message (message.id)}
          <li class={cn("min-w-0 rounded-lg border px-3 py-2 text-sm", message.mine ? "border-signal/30 bg-tint" : "bg-raised/40")}>
            <p class="flex justify-between gap-3 text-xs text-muted-foreground">
              <span class="font-medium text-foreground">{message.who}</span><span class="tabular-nums">{message.at}</span>
            </p>
            <p class={cn("mt-0.5 break-words [overflow-wrap:anywhere]", full ? "whitespace-pre-line" : "truncate")}>{message.text}</p>
          </li>
        {/each}
      </ol>
      {#if before !== null}
        <div bind:this={end} class="pt-2">
          <Button variant="ghost" size="sm" class="w-full" disabled={loading} onclick={load}>
            {loading ? "Loading…" : failed ? "Couldn't load older messages. Try again" : "Load older messages"}
          </Button>
        </div>
      {/if}
    </div>
  {/if}
</section>
