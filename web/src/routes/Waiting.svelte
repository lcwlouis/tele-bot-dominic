<script lang="ts">
  import Code from "$lib/components/code/Code.svelte";
  import { onMount } from "svelte";
  import type { WaitingData, WaitingCard } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import GoneNote from "$lib/components/kit/GoneNote.svelte";
  import { LINKED, linkedId, reveal } from "$lib/deep-link";
  import DiffLines from "$lib/components/memory/DiffLines.svelte";
  import Notices from "$lib/components/system/Notices.svelte";
  import TrialNote from "$lib/components/system/TrialNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Checkbox } from "$lib/components/ui/checkbox";
  import { Label } from "$lib/components/ui/label";
  import Confirm from "$lib/components/work/Confirm.svelte";
  import { cn } from "$lib/utils";
  import { WorkPage } from "$lib/work.svelte";

  // Everything that needs the owner's yes or no: strangers' chats, imports, soul and memory edits, skills, plugin
  // drafts, LAN changes and messages to other chats. Each write answers with the fresh list, so nothing shows stale.
  const KINDS: Record<string, string> = {
    soul_edit: "Soul edit",
    skill: "Skill",
    plugin: "Plugin",
    memory_fact: "Memory fact",
    memory_edit: "Memory edit",
    lan_address: "LAN address",
    cross_chat: "Message to another chat",
    chat_approval: "Chat approval",
  };

  const page = new WorkPage<WaitingData>("/api/waiting");
  let kind = $state("");
  let source = $state("");
  let picked = $state<Record<number, boolean>>({}); // fact id -> unchecked; facts start checked
  let rejecting = $state<number | null>(null);
  let deleting = $state<WaitingData["settled"][number] | null>(null);

  // A link may name one proposal (?proposal=<id>, from a Telegram notice): select its card in place, or its line
  // under Settled once decided. An import's card answers for each of its facts.
  const linked = linkedId("proposal");
  let gone = $state(false);
  let settledOpen = $state(false);
  const holds = (card: WaitingCard, id: number) => card.id === id || (card.type === "import" && card.facts.some((fact) => fact.id === id));
  onMount(async () => {
    await page.load(linked ? `?id=${linked}` : ""); // so a settled one older than the latest still comes
    if (linked === null || page.data === null) return;
    const card = page.data.cards.find((item) => holds(item, linked));
    if (card) reveal(`waiting-${card.type}-${card.id}`);
    else if (page.data.settled.some((row) => row.id === linked)) {
      settledOpen = true;
      reveal(`settled-${linked}`);
    } else gone = true;
  });

  const filter = () => ({ kind, source });
  const reload = () => page.load(`?${new URLSearchParams({ kind, source })}`);
  const chosen = (card: Extract<WaitingCard, { type: "import" }>) => card.facts.filter((fact) => picked[fact.id] !== false).map((fact) => fact.id);
  const select = "h-10 rounded-lg border border-input bg-transparent px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50";
</script>

<PageHeader kicker="Home" title="Waiting for you" subtitle="Everything Dom won't do until you say so." />

{#if gone}<GoneNote text="That proposal is gone. It may have been deleted." ondismiss={() => (gone = false)} />{/if}

{#if page.data === null && page.failed}
  <StatusNote tone="error" title="Couldn't load what's waiting" detail={`Dom's dashboard answered: ${page.failed}.`}>
    <Button variant="outline" onclick={() => page.load()}>Try again</Button>
  </StatusNote>
{:else if page.data === null}
  <p class="sr-only" role="status">Loading…</p>
  <div class="h-40 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  {@const data = page.data}
  {#if data.sources.length}
    <div class="mb-5 flex flex-wrap gap-4" role="group" aria-label="Filter what's waiting">
      <div class="grid gap-1.5">
        <Label for="waiting-kind">Kind</Label>
        <select id="waiting-kind" class={select} bind:value={kind} onchange={reload}>
          <option value="">All kinds</option>
          {#each data.kinds as item (item)}<option value={item}>{KINDS[item] ?? item}</option>{/each}
        </select>
      </div>
      <div class="grid gap-1.5">
        <Label for="waiting-source">Source</Label>
        <select id="waiting-source" class={select} bind:value={source} onchange={reload}>
          <option value="">All sources</option>
          {#each data.sources as item (item)}<option value={item}>{item}</option>{/each}
        </select>
      </div>
    </div>
  {/if}

  {#if data.cards.length === 0 && data.chats.length === 0}
    <StatusNote title="Nothing is waiting for you" detail="Proposals and approvals land here." />
  {:else}
    <p class="mb-3 text-sm text-muted-foreground" role="status">{data.count} {data.count === 1 ? "thing is" : "things are"} waiting.</p>
    <ul class="grid gap-4 lg:grid-cols-2" aria-label="Waiting for you">
      {#each data.chats as chat (chat.id)}
        <li>
          <Card.Root class="h-full">
            <Card.Header>
              <Card.Title class="text-base">{chat.label}</Card.Title>
              <Card.Description class="break-words">{chat.title}</Card.Description>
            </Card.Header>
            <Card.Content class="grid gap-3 text-sm">
              {#if chat.detail}<p class="text-muted-foreground">{chat.detail}</p>{/if}
              {#if chat.first}<p class="text-muted-foreground">First message: {chat.first}</p>{/if}
              <div class="flex flex-wrap gap-2">
                <Button disabled={page.busy} onclick={() => page.write("/api/waiting/chat", { chat_id: chat.id, decision: "approve", ...filter() })}>Approve</Button>
                <Button variant="destructive" disabled={page.busy} onclick={() => page.write("/api/waiting/chat", { chat_id: chat.id, decision: "ignore", ...filter() })}>Ignore</Button>
              </div>
            </Card.Content>
          </Card.Root>
        </li>
      {/each}

      {#each data.cards as card (card.type + card.id)}
        <li id={`waiting-${card.type}-${card.id}`} tabindex="-1" class={linked !== null && holds(card, linked) ? LINKED : "rounded-xl outline-none"}>
          <Card.Root class="h-full">
            {#if card.type === "import"}
              {@const ids = chosen(card)}
              <Card.Header>
                <Card.Title class="text-base">
                  {card.facts.length} {card.facts.length === 1 ? "memory" : "memories"} imported from {card.service}
                </Card.Title>
                <Card.Description>Tick the ones to keep. They go to your private USER.md.</Card.Description>
              </Card.Header>
              <Card.Content class="grid gap-4">
                <ul class="grid max-h-64 gap-2 overflow-y-auto">
                  {#each card.facts as fact (fact.id)}
                    <li class="flex items-start gap-2.5">
                      <Checkbox id={`fact-${fact.id}`} class="mt-0.5" checked={picked[fact.id] !== false} onCheckedChange={(value) => (picked[fact.id] = value)} />
                      <Label for={`fact-${fact.id}`} class="items-start text-sm leading-snug font-normal break-words">{fact.text}</Label>
                    </li>
                  {/each}
                </ul>
                <div class="flex flex-wrap gap-2">
                  <Button disabled={page.busy || ids.length === 0} onclick={() => page.write("/api/waiting/import", { id: card.id, decision: "selected", selected: ids, ...filter() })}>Approve selected</Button>
                  <Button variant="secondary" disabled={page.busy} onclick={() => page.write("/api/waiting/import", { id: card.id, decision: "all", ...filter() })}>Approve all</Button>
                  <Button variant="destructive" disabled={page.busy} onclick={() => (rejecting = card.id)}>Reject all</Button>
                </div>
              </Card.Content>
            {:else}
              <Card.Header>
                <Card.Title class="text-base">{card.label}</Card.Title>
                <Card.Description class="break-words">{card.title}</Card.Description>
              </Card.Header>
              <Card.Content class="grid gap-3 text-sm">
                {#if card.kind === "soul_edit" || card.kind === "memory_edit"}
                  {#if card.diff?.length}<DiffLines lines={card.diff} language="markdown" />{/if}
                {:else if card.kind === "skill"}
                  <p>{card.description}</p>
                  <Code text={card.text ?? ""} language="markdown" focusable class="max-h-64 overflow-auto rounded-lg border bg-raised/50 p-3 text-[0.8rem] break-words whitespace-pre-wrap" />
                  {#if card.diff?.length}<DiffLines lines={card.diff} language="markdown" />{/if}
                {:else if card.kind === "plugin"}
                  <p>{card.description}</p>
                  <Notices network={!!card.network} storage={!!card.storage} notices={data.notices} storageOff />
                  <TrialNote trial={card.test_run ?? null} pending />
                {:else if card.kind === "lan_address"}
                  <p><code class="font-mono">{card.old}</code> → <code class="font-mono">{card.new}</code>{card.address_name ? ` (${card.address_name})` : ""}</p>
                  {#if card.places?.length}<p>Used in {card.places.join(", ")}.</p>{/if}
                  {#if card.macs?.length}<p class="text-muted-foreground">Device {card.macs.join(", ")}</p>{/if}
                  <p class="text-muted-foreground">Dom uses the new address after his next restart.</p>
                {:else if card.kind === "cross_chat"}
                  <pre class="max-h-64 overflow-auto rounded-lg border bg-raised/50 p-3 font-mono text-[0.8rem] break-words whitespace-pre-wrap">{card.text}</pre>
                  <p>{card.reason}</p>
                  <p class="text-muted-foreground">
                    {card.leak_score == null ? "Leak check unavailable" : `Leak score ${card.leak_score}/3`} · Expires in 2 h
                  </p>
                {:else}
                  <p class="break-words">{card.text}</p>
                {/if}
                {#if card.kind === "plugin"}
                  <div class="flex flex-wrap gap-2"><Button href={card.review} variant="secondary">Review code</Button></div>
                {:else}
                  <div class="flex flex-wrap gap-2">
                    <Button disabled={page.busy} onclick={() => page.write("/api/waiting/decide", { id: card.id, decision: "approve", ...filter() })}>Approve</Button>
                    <Button variant="destructive" disabled={page.busy} onclick={() => page.write("/api/waiting/decide", { id: card.id, decision: "reject", ...filter() })}>Reject</Button>
                  </div>
                {/if}
              </Card.Content>
            {/if}
          </Card.Root>
        </li>
      {/each}
    </ul>
  {/if}

  {#if data.settled.length}
    <details class="mt-8 rounded-xl border bg-surface px-4 py-3" bind:open={settledOpen}>
      <summary class="cursor-pointer font-medium">Settled ({data.settled.length})</summary>
      <ul class="mt-3 grid gap-2" aria-label="Settled proposals">
        {#each data.settled as row (row.id)}
          <li id={`settled-${row.id}`} tabindex="-1" class={cn("flex flex-wrap items-center gap-x-3 gap-y-2 border-t pt-2 outline-none first:border-t-0 first:pt-0", row.id === linked && "rounded-md ring-2 ring-signal/70")}>
            <div class="min-w-0 flex-1">
              <p class="flex flex-wrap items-center gap-2 text-sm font-medium">{row.label} <Badge variant={row.status === "approved" ? "secondary" : "outline"}>{row.status}</Badge></p>
              <p class="text-sm break-words text-muted-foreground"><span class="font-mono">#{row.id}</span> {row.title}</p>
            </div>
            <Button variant="destructive" size="sm" disabled={page.busy} aria-label={`Delete settled proposal ${row.id}`} onclick={() => (deleting = row)}>Delete</Button>
          </li>
        {/each}
      </ul>
    </details>
  {/if}
{/if}

<Confirm
  bind:open={() => rejecting !== null, (open) => !open && (rejecting = null)}
  title="Reject every imported memory?"
  description="Dom drops every fact still waiting in this import. You can import the file again later."
  keep="Keep them"
  action="Reject all"
  onconfirm={() => rejecting !== null && page.write("/api/waiting/import", { id: rejecting, decision: "reject", ...filter() })}
/>
<Confirm
  bind:open={() => deleting !== null, (open) => !open && (deleting = null)}
  title={`Delete settled proposal #${deleting?.id}?`}
  description="A copy is saved to the deletion archive."
  keep="Keep it"
  action="Delete proposal"
  onconfirm={() => deleting && page.write("/api/waiting/delete", { id: deleting.id, expected: deleting.created_at, ...filter() })}
/>
