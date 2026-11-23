<script lang="ts">
  import Moon from "@lucide/svelte/icons/moon";
  import Sun from "@lucide/svelte/icons/sun";
  import MessageCircle from "@lucide/svelte/icons/message-circle";
  import Sparkles from "@lucide/svelte/icons/sparkles";
  import { onMount } from "svelte";
  import { getJSON, send, type Chat, type ChatsData } from "$lib/api";
  import ChatMessages from "$lib/components/ChatMessages.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
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
  let dragging = $state<Record<string, number>>({}); // `${chat}:${trait}` -> value while a slider moves
  let leaving = $state<Chat | null>(null);
  let resetting = $state<Chat | null>(null);
  let removing = $state(false);
  let sticker = $state("");

  async function load() {
    try {
      data = await getJSON<ChatsData>("/api/chats");
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(load);

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
        <Label id="everywhere-label">Everywhere <span class="font-normal text-muted-foreground">(a chat's own preset wins)</span></Label>
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
                {#if chat.scope === "group"}
                  <Button variant="destructive" disabled={busy} aria-label={`Leave ${chat.title}`} onclick={() => (leaving = chat)}>Leave group</Button>
                {/if}
              </div>

              {#if reading === chat.id}
                <ChatMessages chatId={chat.id} title={chat.title} />
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

  <Card.Root class="mt-6" aria-labelledby="stickers-title">
    <Card.Header>
      <Card.Title id="stickers-title" class="text-lg">Sticker sets</Card.Title>
      <Card.Description>The sets Dom picks stickers from.</Card.Description>
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
