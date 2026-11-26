<script lang="ts">
  import EyeOff from "@lucide/svelte/icons/eye-off";
  import Moon from "@lucide/svelte/icons/moon";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import Unplug from "@lucide/svelte/icons/unplug";
  import Sun from "@lucide/svelte/icons/sun";
  import MessageCircle from "@lucide/svelte/icons/message-circle";
  import Sparkles from "@lucide/svelte/icons/sparkles";
  import { onMount } from "svelte";
  import { getJSON, send, type Chat, type ChatsData, type UnreachableChat } from "$lib/api";
  import ChatMedia from "$lib/components/ChatMedia.svelte";
  import ChatMessages from "$lib/components/ChatMessages.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import { Checkbox } from "$lib/components/ui/checkbox";
  import * as AlertDialog from "$lib/components/ui/alert-dialog";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { Label } from "$lib/components/ui/label";
  import * as Select from "$lib/components/ui/select";
  import { Slider } from "$lib/components/ui/slider";
  import type { Destination } from "$lib/nav";
  import { toast } from "$lib/toast.svelte";
  import { cn } from "$lib/utils";

  let { destination }: { destination: Destination } = $props();

  type Written = ChatsData & { message: string; changed?: boolean };

  let data = $state<ChatsData | null>(null);
  let failed = $state("");
  let busy = $state(false); // one write at a time: each answer replaces the page data
  let openChat = $state<number | null>(null); // the chat whose personality is showing
  let reading = $state<number | null>(null); // the chat whose recent messages are showing
  let mediaOpen = $state<number | null>(null); // the chat whose media used and left is showing
  let dragging = $state<Record<string, number>>({}); // `${chat}:${trait}` -> value while a slider moves
  let leaving = $state<Chat | null>(null);
  let resetting = $state<Chat | null>(null);
  let removing = $state(false);
  let sticker = $state("");
  // Not reachable (E74): chats the current bot can no longer reach, hidden ones included so they can come back.
  let unreachable = $state<UnreachableChat[]>([]);
  let forgetting = $state<UnreachableChat | null>(null);
  let wipe = $state(false); // "Delete history too", which asks a second time
  let wiping = $state<UnreachableChat | null>(null);
  let freshening = $state<Chat | null>(null);
  let freshened = $state<number | null>(null); // the chat just started fresh, which offers Undo
  let readingGone = $state<number | null>(null);

  async function loadUnreachable() {
    try {
      unreachable = (await getJSON<{ unreachable: UnreachableChat[] }>("/api/chats/unreachable?include_hidden=true")).unreachable;
    } catch {
      // The section keeps what it showed; the main list's error covers a dashboard that isn't answering.
    }
  }

  async function load() {
    try {
      data = await getJSON<ChatsData>("/api/chats");
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(() => {
    load();
    loadUnreachable();
  });

  /** Run one write; its answer is the fresh page data. A refused write toasts its reason and changes nothing. */
  async function write(path: string, body: object, tone: (changed?: boolean) => "ok" | "error" = () => "ok"): Promise<boolean> {
    if (busy) return false;
    busy = true;
    try {
      const { message, changed, ...fresh } = await send<Written>(path, body);
      data = fresh;
      toast.show(tone(changed), message);
      return true;
    } catch {
      // the toast says why
      return false;
    } finally {
      busy = false;
    }
  }

  async function checkNow() {
    if (await write("/api/chats/reachability", {})) await loadUnreachable();
  }
  async function hide(chat: UnreachableChat, hidden: boolean) {
    if (await write("/api/chats/hide", { chat_id: chat.id, hidden })) await loadUnreachable();
  }
  async function forget(chat: UnreachableChat, deleteHistory: boolean) {
    if (await write("/api/chats/forget", { chat_id: chat.id, delete_history: deleteHistory, confirm: deleteHistory })) {
      if (readingGone === chat.id) readingGone = null;
      await loadUnreachable();
    }
  }
  async function fresh(chat: Chat, undo: boolean) {
    if (await write("/api/chats/fresh", { chat_id: chat.id, undo })) freshened = undo ? null : chat.id;
  }
  const shown = $derived(unreachable.filter((chat) => !chat.hidden));
  const hidden = $derived(unreachable.filter((chat) => chat.hidden));

  const act = (chat: Chat, action: "wake" | "sleep" | "leave") => write("/api/chats/action", { chat_id: chat.id, action });
  const setPreset = (chat: Chat | "all", preset: string) => write("/api/chats/preset", { chat_id: chat === "all" ? chat : chat.id, preset });

  async function commit(chat: Chat, name: string, released: number) {
    const key = `${chat.id}:${name}`;
    // The slider's own value can lag a keyboard step by a tick; what it just reported through bind is current.
    await write("/api/chats/traits", { chat_id: chat.id, traits: { [name]: dragging[key] ?? released } });
    delete dragging[key];
  }

  async function addOrRemove(action: "add" | "remove") {
    const name = sticker.trim();
    if (!name) return;
    const ok = await write("/api/stickers", { action, name }, (changed) => (changed ? "ok" : "error"));
    if (ok) sticker = "";
  }

  const everywhereLabel = $derived(
    data?.everywhere === "auto" ? "Auto (Dom picks daily)" : (data?.presets.find((item) => item.name === data?.everywhere)?.title ?? "None"),
  );
  const scopeLabel = { owner_dm: "Owner DM", friend_dm: "Friend DM", group: "Group" } as const;
  const stateWord = { online: "Online", asleep: "Asleep", offline: "Offline" } as const;
  const trait = (name: string) => name.charAt(0).toUpperCase() + name.slice(1);
  const changed = (chat: Chat) => chat.sliders.filter((slider) => slider.custom).length;
</script>

<PageHeader kicker="Conversations" title="Chats" subtitle={destination.blurb}>
  {#snippet aside()}
    {#if data && data.pending}
      <Button href="/app/waiting" variant="secondary" size="sm">{data.pending} waiting for approval</Button>
    {/if}
  {/snippet}
</PageHeader>

{#if data === null && failed}
  <StatusNote tone="error" title="Couldn't load chats" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="secondary" onclick={load}>Retry</Button>
  </StatusNote>
{:else if data === null}
  <p class="sr-only" role="status">Loading chats…</p>
  <div class="grid gap-4" aria-hidden="true">
    {#each [0, 1, 2] as index (index)}<div class="h-28 animate-pulse rounded-xl border bg-surface"></div>{/each}
  </div>
{:else}
  {@const chats = data}
  <Card.Root class="mb-6" aria-labelledby="vibe-title">
    <Card.Content class="grid gap-5 md:grid-cols-[minmax(0,1fr)_16rem] md:items-end">
      <div>
        <p class="kicker flex items-center gap-2"><Sparkles class="size-3.5 text-signal" aria-hidden="true" /> Vibe everywhere</p>
        <h2 id="vibe-title" class="mt-2 text-xl font-semibold tracking-tight">
          {#if chats.vibe.mode === "none"}No preset everywhere{:else}{chats.vibe.title || "His usual self"}{/if}
        </h2>
        <p class="mt-1.5 text-sm text-muted-foreground">
          {#if chats.vibe.mode === "auto"}
            Dom picks his own vibe each day{chats.vibe.source === "switch" ? " (switched mid-day)" : ""}{chats.vibe.since ? ` · ${chats.vibe.since}` : ""}{chats.vibe.reason ? `: ${chats.vibe.reason}` : "."}
          {:else if chats.vibe.mode === "locked"}
            Fixed by you. {chats.vibe.description}
          {:else}
            Chats with no preset of their own use Dom's soul as written.
          {/if}
        </p>
      </div>
      <div class="grid gap-1.5">
        <div class="flex items-center gap-1">
          <Label id="everywhere-label">Everywhere</Label>
          <InfoGlyph label="About the everywhere preset">A chat's own preset wins over this one.</InfoGlyph>
        </div>
        <Select.Root type="single" value={chats.everywhere} disabled={busy} onValueChange={(value) => setPreset("all", value)}>
          <Select.Trigger class="h-9 w-full" aria-labelledby="everywhere-label">{everywhereLabel}</Select.Trigger>
          <Select.Content>
            <Select.Item value="" label="None" />
            <Select.Item value="auto" label="Auto (Dom picks daily)" />
            {#each chats.presets as item (item.name)}
              <Select.Item value={item.name} label={item.title} />
            {/each}
          </Select.Content>
        </Select.Root>
      </div>
    </Card.Content>
  </Card.Root>

  {#if chats.chats.length === 0}
    <StatusNote title="Dom doesn't know any chats yet" detail="Chats show up here once you approve them." />
  {:else}
    <ul class="grid gap-3" aria-label="Chats">
      {#each chats.chats as chat (chat.id)}
        {@const expanded = openChat === chat.id}
        <li>
          <Card.Root>
            <Card.Content class="grid gap-4">
              <div class="flex flex-wrap items-center gap-x-4 gap-y-3">
                <span class="grid size-10 shrink-0 place-items-center rounded-lg border bg-raised text-signal" aria-hidden="true">
                  {#if chat.state === "asleep"}<Moon class="size-4" />{:else if chat.state === "online"}<Sun class="size-4" />{:else}<MessageCircle class="size-4" />{/if}
                </span>
                <div class="min-w-0 flex-1 basis-48">
                  <h2 class="truncate text-base font-semibold">{chat.title}</h2>
                  <p class="mt-0.5 text-sm text-muted-foreground">
                    {chat.preset?.title ?? "No preset"} · {stateWord[chat.state]}{chat.until ? ` until ${chat.until}` : ""}
                  </p>
                </div>
                <div class="flex flex-wrap items-center gap-2">
                  <Badge variant="outline">{scopeLabel[chat.scope]}</Badge>
                  <Badge variant={chat.unread ? "secondary" : "outline"} aria-label={`${chat.unread} unread`}>{chat.unread} unread</Badge>
                </div>
              </div>
              {#if chat.task}
                <p class="text-sm text-muted-foreground">
                  <span class="font-medium text-foreground">Task mode:</span> {chat.task.reason} (ends at {chat.task.until} if the chat goes quiet)
                </p>
              {/if}
              <div class="flex flex-wrap items-center gap-2">
                {#if chat.state === "online"}
                  <Button variant="outline" disabled={busy} aria-label={`Send Dom to sleep in ${chat.title}`} onclick={() => act(chat, "sleep")}>Send to sleep</Button>
                {:else}
                  <Button disabled={busy} aria-label={`Wake Dom in ${chat.title}`} onclick={() => act(chat, "wake")}>Wake Dom</Button>
                {/if}
                <Button
                  variant="secondary"
                  aria-expanded={expanded}
                  aria-controls={`personality-${chat.id}`}
                  onclick={() => (openChat = expanded ? null : chat.id)}
                >
                  Personality{changed(chat) ? ` · ${changed(chat)} changed` : ""}
                </Button>
                <Button
                  variant="ghost"
                  aria-expanded={reading === chat.id}
                  aria-label={`Messages in ${chat.title}`}
                  onclick={() => (reading = reading === chat.id ? null : chat.id)}
                >
                  Messages
                </Button>
                <Button
                  variant="ghost"
                  aria-expanded={mediaOpen === chat.id}
                  aria-label={`Media used and left in ${chat.title}`}
                  onclick={() => (mediaOpen = mediaOpen === chat.id ? null : chat.id)}
                >
                  Media
                </Button>
                <!-- The rescue: earlier messages leave the prompt, never the history; the last start can be undone. -->
                <Button variant="ghost" disabled={busy} aria-label={`Fresh start in ${chat.title}`} onclick={() => (freshening = chat)}>Fresh start</Button>
                {#if freshened === chat.id}
                  <Button variant="ghost" disabled={busy} aria-label={`Undo the fresh start in ${chat.title}`} onclick={() => fresh(chat, true)}>Undo fresh start</Button>
                {/if}
                {#if chat.scope === "group"}
                  <Button variant="destructive" disabled={busy} aria-label={`Leave ${chat.title}`} onclick={() => (leaving = chat)}>Leave group</Button>
                {/if}
              </div>

              {#if reading === chat.id}
                <ChatMessages chatId={chat.id} title={chat.title} />
              {/if}
              {#if mediaOpen === chat.id}
                <ChatMedia chatId={chat.id} title={chat.title} />
              {/if}
              {#if expanded}
                <div id={`personality-${chat.id}`} class="grid gap-5 border-t pt-4">
                  <div class="grid gap-2">
                    <p id={`preset-label-${chat.id}`} class="text-sm font-medium">Preset</p>
                    <div role="group" aria-labelledby={`preset-label-${chat.id}`} class="flex flex-wrap gap-2">
                      {#each [{ name: "", title: "Same as everywhere" }, ...chats.presets] as item (item.name)}
                        <Button
                          variant={chat.own_preset === item.name ? "default" : "outline"}
                          size="sm"
                          aria-pressed={chat.own_preset === item.name}
                          disabled={busy}
                          onclick={() => chat.own_preset !== item.name && setPreset(chat, item.name)}
                        >
                          {item.title}
                        </Button>
                      {/each}
                    </div>
                  </div>
                  <div class="grid gap-1">
                    <p class="text-sm font-medium">Personality sliders</p>
                    <ul class="divide-y">
                      {#each chat.sliders as slider (slider.name)}
                        {@const key = `${chat.id}:${slider.name}`}
                        {@const value = dragging[key] ?? slider.value}
                        <li class="grid grid-cols-[6.5rem_minmax(0,1fr)_3rem] items-center gap-3 py-3 sm:grid-cols-[8rem_minmax(0,1fr)_3rem]">
                          <span id={`trait-${key}`} class="text-sm">
                            {trait(slider.name)}{#if slider.custom}<span class="ml-1 text-xs text-signal-strong">(custom)</span>{/if}
                          </span>
                          <Slider
                            type="single"
                            min={0.05}
                            max={0.95}
                            step={0.01}
                            disabled={busy}
                            thumbLabelledby={`trait-${key}`}
                            bind:value={() => value, (next) => (dragging[key] = next)}
                            onValueCommit={(next) => commit(chat, slider.name, next)}
                          />
                          <span class="text-right font-mono text-sm text-muted-foreground" aria-hidden="true">{Math.round(value * 100)}%</span>
                        </li>
                      {/each}
                    </ul>
                    <div>
                      <Button variant="outline" size="sm" class="mt-2" disabled={busy || changed(chat) === 0} onclick={() => (resetting = chat)}>
                        Reset to default
                      </Button>
                    </div>
                  </div>
                </div>
              {/if}
            </Card.Content>
          </Card.Root>
        </li>
      {/each}
    </ul>
  {/if}

  <section aria-labelledby="unreachable-title" class="mt-8 grid gap-3">
    <div class="flex flex-wrap items-center gap-x-1.5 gap-y-2">
      <h2 id="unreachable-title" class="text-title font-semibold">Not reachable</h2>
      <InfoGlyph label="About chats Dom can't reach">
        Chats the current bot can no longer reach, for example after a bot token change. Their history is kept and stays searchable. Hide one to
        tidy this list, or forget it to drop it from Dom's chats.
      </InfoGlyph>
      <Button variant="outline" size="sm" class="ml-auto" disabled={busy} onclick={checkNow}><RefreshCw data-icon="inline-start" aria-hidden="true" /> Check now</Button>
    </div>
    {#if shown.length === 0}
      <p class="rounded-xl border border-dashed px-4 py-3 text-body text-muted-foreground">Dom can reach every chat he knows.</p>
    {:else}
      <ul class="divide-y divide-line/70 rounded-xl border bg-surface" aria-label="Chats Dom can't reach">
        {#each shown as chat (chat.id)}
          <li class="grid gap-3 px-3.5 py-2.5">
            <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
              <Unplug class="size-4 shrink-0 text-status-paused" aria-hidden="true" />
              <div class="min-w-0 flex-1 basis-40">
                <p class="truncate text-body font-medium">{chat.title}</p>
                <p class="text-meta text-muted-foreground">
                  {scopeLabel[chat.scope]}{chat.since ? `, unreachable since ${chat.since}` : ""}{chat.reason ? `: ${chat.reason}` : ""}
                </p>
              </div>
              <div class="flex items-center gap-1">
                <Button
                  variant="ghost"
                  size="sm"
                  aria-expanded={readingGone === chat.id}
                  aria-label={`Messages in ${chat.title}`}
                  onclick={() => (readingGone = readingGone === chat.id ? null : chat.id)}
                >
                  Messages
                </Button>
                <IconButton label={`Hide ${chat.title}`} icon={EyeOff} disabled={busy} onclick={() => hide(chat, true)} />
                <Button variant="destructive" size="sm" disabled={busy} aria-label={`Forget ${chat.title}`} onclick={() => ((wipe = false), (forgetting = chat))}>Forget</Button>
              </div>
            </div>
            {#if readingGone === chat.id}<ChatMessages chatId={chat.id} title={chat.title} turns={false} />{/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if hidden.length}
      <details class="group rounded-xl border bg-surface text-body">
        <summary class="flex min-h-10 cursor-pointer items-center px-3.5 text-muted-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/50">Hidden ({hidden.length})</summary>
        <ul class="divide-y divide-line/70 border-t" aria-label="Hidden chats">
          {#each hidden as chat (chat.id)}
            <li class="flex items-center gap-3 px-3.5 py-2">
              <span class="min-w-0 flex-1 truncate">{chat.title}</span>
              <Button variant="ghost" size="sm" disabled={busy} aria-label={`Show ${chat.title} again`} onclick={() => hide(chat, false)}>Show again</Button>
            </li>
          {/each}
        </ul>
      </details>
    {/if}
  </section>

  <Card.Root class="mt-6" aria-labelledby="stickers-title">
    <Card.Header>
      <Card.Title id="stickers-title" class="flex items-center gap-1.5 text-lg">
        Sticker sets <InfoGlyph label="About sticker sets">The sets Dom picks stickers from.</InfoGlyph>
      </Card.Title>
    </Card.Header>
    <Card.Content class="grid gap-4">
      {#if chats.stickers.length}
        <ul class="flex flex-wrap gap-2">
          {#each chats.stickers as name (name)}
            <li>
              <a
                href={`https://t.me/addstickers/${name}`}
                target="_blank"
                rel="noopener noreferrer"
                class="inline-flex min-h-9 items-center rounded-lg border bg-raised px-3 font-mono text-sm text-signal-strong outline-none hover:border-signal/60 focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                {name}<span class="sr-only"> (opens in Telegram)</span>
              </a>
            </li>
          {/each}
        </ul>
      {:else}
        <p class="text-sm text-muted-foreground">No sticker sets yet. Add one by its name or share link.</p>
      {/if}
      <form class="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-end" onsubmit={(event) => (event.preventDefault(), addOrRemove("add"))}>
        <div class="grid gap-1.5">
          <Label for="sticker-name">Set name or link</Label>
          <Input id="sticker-name" bind:value={sticker} maxlength={200} />
        </div>
        <Button type="submit" variant="secondary" disabled={busy || !sticker.trim()}>Add</Button>
        <Button type="button" variant="destructive" disabled={busy || !sticker.trim()} onclick={() => (removing = true)}>Remove</Button>
      </form>
      {#if chats.stickers.length}
        <div>
          <Button variant="outline" size="sm" disabled={busy} onclick={() => write("/api/stickers/refresh", {})}>Refresh descriptions</Button>
        </div>
      {/if}
    </Card.Content>
  </Card.Root>
{/if}

<AlertDialog.Root open={leaving !== null} onOpenChange={(open) => !open && (leaving = null)}>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Leave {leaving?.title}?</AlertDialog.Title>
      <AlertDialog.Description>Friends will see Dom leave the group.</AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">Stay</AlertDialog.Cancel>
      <AlertDialog.Action variant="destructive-solid" onclick={() => { const chat = leaving; leaving = null; chat && act(chat, "leave"); }}>Leave group</AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<AlertDialog.Root open={resetting !== null} onOpenChange={(open) => !open && (resetting = null)}>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Reset {resetting?.title} to its default personality?</AlertDialog.Title>
      <AlertDialog.Description>The sliders you changed for this chat go back to its preset and Dom's soul.</AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">Keep my changes</AlertDialog.Cancel>
      <AlertDialog.Action
        variant="destructive-solid"
        onclick={() => { const chat = resetting; resetting = null; chat && write("/api/chats/traits/reset", { chat_id: chat.id }); }}
      >
        Reset to default
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<AlertDialog.Root open={removing} onOpenChange={(open) => !open && (removing = false)}>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Remove this sticker set?</AlertDialog.Title>
      <AlertDialog.Description>Dom stops using {sticker.trim()} for stickers.</AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">Keep it</AlertDialog.Cancel>
      <AlertDialog.Action variant="destructive-solid" onclick={() => { removing = false; addOrRemove("remove"); }}>Remove set</AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<AlertDialog.Root open={forgetting !== null} onOpenChange={(open) => !open && (forgetting = null)}>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Forget {forgetting?.title}?</AlertDialog.Title>
      <AlertDialog.Description>Dom drops it from his chats. Its history is kept and stays searchable unless you delete it too.</AlertDialog.Description>
    </AlertDialog.Header>
    <div class="flex items-start gap-2.5">
      <Checkbox id="forget-wipe" class="mt-0.5" bind:checked={wipe} />
      <Label for="forget-wipe" class="items-start leading-snug font-normal">Delete history too</Label>
    </div>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">Keep it</AlertDialog.Cancel>
      <AlertDialog.Action
        variant="destructive-solid"
        onclick={() => {
          const chat = forgetting;
          forgetting = null;
          if (!chat) return;
          if (wipe) wiping = chat; // the second, explicit confirm
          else forget(chat, false);
        }}
      >
        Forget chat
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<AlertDialog.Root open={wiping !== null} onOpenChange={(open) => !open && (wiping = null)}>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Delete every message from {wiping?.title}?</AlertDialog.Title>
      <AlertDialog.Description>This can't be undone. Dom forgets the chat and its history is deleted, so search won't find it either.</AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">Keep the history</AlertDialog.Cancel>
      <AlertDialog.Action variant="destructive-solid" onclick={() => { const chat = wiping; wiping = null; chat && forget(chat, true); }}>Delete history and forget</AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<AlertDialog.Root open={freshening !== null} onOpenChange={(open) => !open && (freshening = null)}>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Start {freshening?.title} fresh?</AlertDialog.Title>
      <AlertDialog.Description>Earlier messages leave Dom's prompt; nothing is deleted.</AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">Not now</AlertDialog.Cancel>
      <AlertDialog.Action onclick={() => { const chat = freshening; freshening = null; chat && fresh(chat, false); }}>Start fresh</AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>
