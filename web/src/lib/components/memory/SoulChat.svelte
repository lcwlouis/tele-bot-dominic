<script lang="ts" module>
  /** One message in the soul conversation. Dom's may carry the edit he filed; the owner's may have failed to send. */
  export type SoulTurn = {
    role: "owner" | "dom";
    text: string;
    proposal?: { id: number; diff: string[] } | null;
    failed?: string;
  };

  // Outlives the panel, like the transcript: closing and reopening while Dom is thinking must not allow a second send.
  export const soulRequest = $state({ thinking: false });
</script>

<script lang="ts">
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import ArrowLeft from "@lucide/svelte/icons/arrow-left";
  import RotateCcw from "@lucide/svelte/icons/rotate-ccw";
  import SendHorizontal from "@lucide/svelte/icons/send-horizontal";
  import { onMount, tick } from "svelte";
  import { getJSON, send, ApiError, type MemoryFile, type MemoryIndex, type MemoryWaiting } from "$lib/api";
  import Markdown from "$lib/components/markdown/Markdown.svelte";
  import DiffLines from "$lib/components/memory/DiffLines.svelte";
  import Orbit from "$lib/components/Orbit.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Button } from "$lib/components/ui/button";
  import { Label } from "$lib/components/ui/label";
  import { toast } from "$lib/toast.svelte";
  import { cn } from "$lib/utils";

  // Talking SOUL.md over with Dom. The page keeps the conversation (`turns` is bound, so it survives closing the
  // panel) and sends it whole with each message; the server caps it. Dom's edits wait as proposals, decided here.
  let {
    index,
    turns = $bindable([]),
    onwaiting,
    onclose,
  }: { index: MemoryIndex; turns: SoulTurn[]; onwaiting: (result: MemoryWaiting) => void; onclose: () => void } = $props();

  let message = $state("");
  let tooLong = $state("");
  let deciding = $state(false);
  let decided = $state<Record<number, string>>({}); // proposal id -> "Approved" / "Rejected"
  let soul = $state<MemoryFile | null>(null);
  let soulError = $state("");
  let log = $state<HTMLOListElement | null>(null);

  const pending = $derived(new Set(index.edits.map((edit) => edit.id)));
  const total = $derived(turns.reduce((sum, turn) => sum + turn.text.length, 0));
  // Room for the owner's next message and Dom's reply, within the server's caps.
  const full = $derived(turns.length + 1 >= index.max_soul_turns || total >= index.max_soul_chat);
  const count = (n: number) => n.toLocaleString("en-US");

  async function loadSoul() {
    try {
      soul = await getJSON<MemoryFile>("/api/memory/file?file=SOUL.md");
      soulError = "";
    } catch (error) {
      soulError = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(() => {
    loadSoul();
    document.getElementById("soul-message")?.focus();
  });

  async function scrollDown() {
    await tick();
    log?.lastElementChild?.scrollIntoView({ block: "nearest" });
  }

  async function ask() {
    if (soulRequest.thinking) return;
    soulRequest.thinking = true;
    for (const turn of turns) delete turn.failed; // a new message or a retry resends whatever failed before
    const asked = turns.at(-1);
    scrollDown();
    try {
      const result = await send<{ reply: string; proposal: SoulTurn["proposal"] } & MemoryWaiting>(
        "/api/memory/soul",
        { messages: turns.map(({ role, text }) => ({ role, text })) },
        { quiet: true },
      );
      turns.push({ role: "dom", text: result.reply, proposal: result.proposal });
      onwaiting(result);
    } catch (error) {
      if (asked) asked.failed = error instanceof ApiError ? error.message : "Couldn't reach Dom. Try again.";
    } finally {
      soulRequest.thinking = false;
      scrollDown();
    }
  }

  function submit() {
    const text = message.trim();
    if (!text || soulRequest.thinking || full) return;
    if (total + text.length > index.max_soul_chat) {
      // The server would refuse the whole conversation; keep the text editable instead of appending it.
      tooLong = `That's longer than this conversation has room for (${count(index.max_soul_chat - total)} characters left). Shorten it or start a new conversation.`;
      return;
    }
    tooLong = "";
    turns.push({ role: "owner", text });
    message = "";
    ask();
  }

  function keydown(event: KeyboardEvent) {
    // Enter sends, Shift+Enter is a new line, as in Telegram.
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  async function decide(id: number, decision: "approve" | "reject") {
    if (deciding) return;
    deciding = true;
    try {
      const result = await send<MemoryWaiting & { message: string }>("/api/memory/proposal", { id, decision });
      decided[id] = result.message;
      onwaiting(result);
      toast.show("ok", decision === "approve" ? "SOUL.md updated." : "Proposal rejected.");
      if (decision === "approve") await loadSoul();
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) onwaiting(await getJSON<MemoryWaiting>("/api/memory")); // handled elsewhere
    } finally {
      deciding = false;
    }
  }

  function restart() {
    tooLong = "";
    turns = [];
    message = "";
    document.getElementById("soul-message")?.focus();
  }
</script>

<section aria-labelledby="soul-title" class="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,25rem)]">
  <div class="flex min-w-0 flex-col rounded-xl border bg-surface">
    <header class="flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3.5 sm:px-5">
      <div class="min-w-0">
        <Button variant="ghost" size="sm" class="mb-1 -ml-2 w-fit" onclick={onclose}>
          <ArrowLeft aria-hidden="true" /> Files
        </Button>
        <h2 id="soul-title" class="text-lg font-semibold tracking-tight">Talk to Dom about his soul</h2>
        <p class="mt-0.5 text-sm text-muted-foreground">Talk it through. When Dom has a change in mind he proposes it here, and SOUL.md changes only when you approve.</p>
      </div>
      {#if turns.length}
        <Button variant="outline" size="sm" disabled={soulRequest.thinking} onclick={restart}><RotateCcw aria-hidden="true" /> New conversation</Button>
      {/if}
    </header>

    <!-- On a phone SOUL.md folds above the conversation; on a desktop it sits beside it. -->
    <details class="group border-b px-4 py-3 lg:hidden">
      <summary class="cursor-pointer text-sm font-medium">SOUL.md as it is now</summary>
      <div class="mt-3 max-h-80 overflow-y-auto">
        {#if soul}<Markdown text={soul.content} class="text-sm" />{:else if soulError}<p class="text-sm text-coral">{soulError}</p>{:else}<p class="text-sm text-muted-foreground">Loading SOUL.md…</p>{/if}
      </div>
    </details>

    <ol bind:this={log} aria-label="Conversation" class="grid min-h-48 content-start gap-4 px-4 py-5 sm:px-5 lg:max-h-[34rem] lg:overflow-y-auto">
      {#if turns.length === 0}
        <li class="flex items-start gap-3 text-sm text-muted-foreground">
          <Orbit size="sm" activity="idle" />
          <p class="pt-1">Ask what Dom thinks of a line, how he'd like to come across, or something you'd change about him.</p>
        </li>
      {/if}
      {#each turns as turn, at (at)}
        {#if turn.role === "owner"}
          <li class="grid justify-items-end gap-1.5">
            <p class="sr-only">You said</p>
            <p class={cn("max-w-[85%] rounded-2xl rounded-br-md px-3.5 py-2.5 text-sm break-words whitespace-pre-wrap", turn.failed ? "border border-coral/45 bg-coral-tint" : "bg-tint")}>{turn.text}</p>
            {#if turn.failed}
              <div role="alert" class="flex max-w-[85%] flex-wrap items-center justify-end gap-2 text-sm text-coral">
                <span>{turn.failed}</span>
                {#if at === turns.length - 1}<Button variant="outline" size="sm" disabled={soulRequest.thinking} onclick={ask}>Retry</Button>{/if}
              </div>
            {/if}
          </li>
        {:else}
          <li class="grid justify-items-start gap-2">
            <p class="sr-only">Dom said</p>
            <div class="max-w-[92%] rounded-2xl rounded-bl-md border bg-raised px-3.5 py-2.5"><Markdown text={turn.text} class="text-sm" /></div>
            {#if turn.proposal}
              {@const id = turn.proposal.id}
              {@const outcome = decided[id] ?? (pending.has(id) ? "" : "Handled")}
              <div class="grid w-full max-w-[92%] gap-3 rounded-xl border border-signal/35 bg-canvas/50 p-3">
                <p class="kicker text-signal">Proposed change to SOUL.md</p>
                <DiffLines lines={turn.proposal.diff} language="markdown" />
                {#if outcome}
                  <p class="text-sm text-muted-foreground">{outcome === "Handled" ? "Already decided." : `${outcome}.`}</p>
                {:else}
                  <div class="flex flex-wrap gap-2">
                    <Button size="sm" disabled={deciding} onclick={() => decide(id, "approve")}>Approve</Button>
                    <Button size="sm" variant="outline" disabled={deciding} onclick={() => decide(id, "reject")}>Reject</Button>
                  </div>
                {/if}
              </div>
            {/if}
          </li>
        {/if}
      {/each}
      {#if soulRequest.thinking}
        <li class="flex items-center gap-3 text-sm text-muted-foreground">
          <Orbit size="sm" activity="replying" />
          <span role="status">Dom is thinking…</span>
        </li>
      {/if}
    </ol>

    <form class="grid gap-2 border-t px-4 py-3.5 sm:px-5" onsubmit={(event) => (event.preventDefault(), submit())}>
      <Label for="soul-message" class="sr-only">Message to Dom</Label>
      {#if full}
        <p class="text-sm text-muted-foreground">This conversation is as long as it can get. Start a new one to keep going.</p>
      {/if}
      <div class="flex items-end gap-2">
        <textarea
          id="soul-message"
          rows="2"
          placeholder="Message Dom"
          maxlength={index.max_soul_request}
          class="max-h-48 min-h-11 w-full resize-y rounded-[0.65rem] border border-input bg-canvas/50 px-3 py-2.5 text-sm outline-none transition-colors placeholder:text-muted-foreground hover:border-muted-foreground/60 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
          aria-describedby="soul-hint"
          disabled={full}
          bind:value={message}
          onkeydown={keydown}
        ></textarea>
        <IconButton type="submit" variant="default" size="icon" class="size-11 shrink-0" label="Send to Dom" icon={SendHorizontal} disabled={soulRequest.thinking || full || !message.trim()} />
      </div>
      {#if tooLong}<p role="alert" class="text-xs text-coral">{tooLong}</p>{/if}
      <p id="soul-hint" class="text-xs text-muted-foreground">Enter sends, Shift+Enter starts a new line · {count(message.length)} of {count(index.max_soul_request)} characters</p>
    </form>
  </div>

  <aside aria-labelledby="soul-preview-title" class="hidden rounded-xl border bg-surface lg:sticky lg:top-4 lg:block">
    <header class="flex items-baseline justify-between gap-2 border-b px-5 py-3.5">
      <h3 id="soul-preview-title" class="font-mono text-base font-semibold">SOUL.md</h3>
      <span class="text-xs text-muted-foreground">As it is now</span>
    </header>
    <div class="max-h-[38rem] overflow-y-auto px-5 py-4">
      {#if soul}
        <Markdown text={soul.content} class="text-sm" />
      {:else if soulError}
        <StatusNote tone="error" title="Couldn't load SOUL.md" detail={soulError}>
          <Button variant="secondary" onclick={loadSoul}>Retry</Button>
        </StatusNote>
      {:else}
        <p class="sr-only" role="status">Loading SOUL.md…</p>
        <div class="h-64 animate-pulse rounded-lg bg-raised" aria-hidden="true"></div>
      {/if}
    </div>
  </aside>
</section>
