<script lang="ts">
  import CodeEditor from "$lib/components/code/CodeEditor.svelte";
  import ArrowLeft from "@lucide/svelte/icons/arrow-left";
  import Archive from "@lucide/svelte/icons/archive";
  import Bot from "@lucide/svelte/icons/bot";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import Pin from "@lucide/svelte/icons/pin";
  import Trash from "@lucide/svelte/icons/trash-2";
  import UserRound from "@lucide/svelte/icons/user-round";
  import Copy from "@lucide/svelte/icons/copy";
  import Download from "@lucide/svelte/icons/download";
  import FileText from "@lucide/svelte/icons/file-text";
  import Lock from "@lucide/svelte/icons/lock";
  import Search from "@lucide/svelte/icons/search";
  import Sparkles from "@lucide/svelte/icons/sparkles";
  import Eye from "@lucide/svelte/icons/eye";
  import PenLine from "@lucide/svelte/icons/pen-line";
  import Upload from "@lucide/svelte/icons/upload";
  import X from "@lucide/svelte/icons/x";
  import { onMount, tick } from "svelte";
  import { getJSON, send, ApiError, type CoreMemories, type CoreMemory, type MemoryFile, type MemoryIndex, type MemoryWaiting } from "$lib/api";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import * as Sheet from "$lib/components/ui/sheet";
  import { setMemoryTags, stripTags } from "$lib/memory-tags";
  import DiffLines from "$lib/components/memory/DiffLines.svelte";
  import SoulChat, { type SoulFile, type SoulTurn } from "$lib/components/memory/SoulChat.svelte";
  import { navigate } from "$lib/router.svelte";
  import Markdown from "$lib/components/markdown/Markdown.svelte";
  import * as Tabs from "$lib/components/ui/tabs";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import * as AlertDialog from "$lib/components/ui/alert-dialog";
  import { Badge } from "$lib/components/ui/badge";
  import { Button, buttonVariants } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Checkbox } from "$lib/components/ui/checkbox";
  import { Input } from "$lib/components/ui/input";
  import { Label } from "$lib/components/ui/label";
  import * as Select from "$lib/components/ui/select";
  import type { Destination, Tab } from "$lib/nav";
  import { cn } from "$lib/utils";
  import { toast } from "$lib/toast.svelte";

  let { destination, tab }: { destination: Destination; tab?: Tab } = $props();

  const SOUL = "SOUL.md";
  const CORE = "core"; // the Core memories view, opened like a file
  const FIRST = "memory/MEMORY.md";
  const MAX_DOCUMENT_BYTES = 1024 * 1024;
  const field =
    "w-full rounded-[0.65rem] border border-input bg-canvas/50 px-3 py-2.5 text-sm outline-none transition-colors placeholder:text-muted-foreground hover:border-muted-foreground/60 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50";

  let index = $state<MemoryIndex | null>(null);
  let failed = $state("");
  // `open` is the file in the editor; null shows the file list alone on a phone. `talking` is the soul conversation.
  let open = $state<string | null>(null);
  // The World page and a link can open the conversation on WORLD.md (?talk=world), and Back then returns there.
  const asked = new URLSearchParams(location.search);
  let talking = $state(asked.has("talk"));
  let talkAbout = $state<SoulFile>(asked.get("talk") === "world" ? "WORLD.md" : "SOUL.md");
  const fromWorld = asked.get("from") === "world";
  function talk(about: SoulFile) {
    guard(() => {
      talkAbout = about;
      talking = true;
    });
  }
  function stopTalking() {
    talking = false;
    if (fromWorld) navigate("/app/memory/world");
    else if (open === null && matchMedia("(min-width: 1024px)").matches) openFile(FIRST);
  }
  let file = $state<MemoryFile | null>(null);
  let fileError = $state("");
  let draft = $state("");
  let view = $state<"preview" | "edit">("preview"); // the open file's tab; the draft survives switching
  let saving = $state(false);
  let saveError = $state("");
  let leaving = $state<(() => void) | null>(null); // a switch waiting on the discard-changes dialog

  const dirty = $derived(file !== null && draft !== file.content);
  const used = $derived(draft.length);
  const over = $derived(file?.limit != null && used > file.limit);

  async function load() {
    try {
      index = await getJSON<MemoryIndex>("/api/memory");
      failed = "";
      const wanted = new URLSearchParams(location.search).get("file"); // a search result opens its file
      if (open === null && wanted) await openFile(wanted);
      else if (open === null && !talking && matchMedia("(min-width: 1024px)").matches) await openFile(FIRST);
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(load);

  function waiting(result: MemoryWaiting) {
    if (index) index = { ...index, imports: result.imports, edits: result.edits };
  }

  // The open file's pane. On a phone the list hides once a file opens, so the pane is brought to the top of the view
  // instead of staying wherever the list had scrolled to.
  let pane = $state<HTMLElement | null>(null);
  async function reveal() {
    await tick();
    if (!pane || matchMedia("(min-width: 1024px)").matches) return;
    if (pane.getBoundingClientRect().top < 0) pane.scrollIntoView({ block: "start", behavior: "instant" });
  }

  async function openFile(path: string) {
    open = path;
    talking = false;
    reveal();
    file = null;
    fileError = "";
    saveError = "";
    try {
      file = await getJSON<MemoryFile>(`/api/memory/file?file=${encodeURIComponent(path)}`);
      draft = file.content;
      view = "preview"; // preview by default; Edit is one tap away
    } catch (error) {
      fileError = error instanceof Error ? error.message : "Unknown error";
    }
  }

  // A journal keeps every line break: its mood, entry and nudges sit on lines of their own.
  const journal = (path: string) => /(^|\/)JOURNAL\.md$|(^|\/)journal\//.test(path);
  // Source tags read as the chat's name in the preview's chips, and `{entities person:7}` as the name the file API
  // gives (`file.entities`), else the person's name from the People list.
  const people = $derived(
    Object.fromEntries(
      (index?.sections.flatMap((section) => section.files) ?? []).flatMap((item) => {
        const id = /^memory\/people\/(\d+)\.md$/.exec(item.path)?.[1];
        return id ? [[`person:${id}`, item.label]] : [];
      }),
    ),
  );
  setMemoryTags(() => ({ chats: index?.chat_names ?? {}, entities: { ...people, ...(file?.entities ?? {}) } }));
  const archived = $derived(new Set(index?.sections.filter((section) => section.archive).flatMap((section) => section.files.map((item) => item.path)) ?? []));
  // The file list's filter: by name or path, groups with nothing left hide, and the folded archive opens.
  let filter = $state("");
  const needle = $derived(filter.trim().toLowerCase());
  const sections = $derived(
    (index?.sections ?? [])
      .map((section) => ({
        ...section,
        files: needle ? section.files.filter((item) => `${item.label} ${item.path}`.toLowerCase().includes(needle)) : section.files,
      }))
      .filter((section) => section.files.length > 0),
  );
  const shownCount = $derived(sections.reduce((total, section) => total + section.files.length, 0));
  const row =
    "flex min-h-10 w-full items-center gap-2.5 rounded-[0.55rem] px-2.5 text-left text-sm text-muted-foreground transition-colors outline-none hover:bg-raised hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 lg:min-h-8";
  const rowHere = "bg-tint text-signal-strong hover:bg-tint hover:text-signal-strong";
  const labelOf = (path: string) => index?.sections.flatMap((section) => section.files).find((item) => item.path === path)?.label ?? path;

  // Core memories (W2): every pinned line, who pinned it and why. Deleting one edits its file through the same save.
  let core = $state<CoreMemories | null>(null);
  let coreError = $state("");
  let dropping = $state<CoreMemory | null>(null);
  let coreBusy = $state(false);

  async function openCore() {
    open = CORE;
    talking = false;
    reveal();
    file = null;
    fileError = "";
    coreError = "";
    try {
      core = await getJSON<CoreMemories>("/api/memory/core");
    } catch (error) {
      coreError = error instanceof Error ? error.message : "Unknown error";
    }
  }

  const PIN = /[ \t]*\{core (?:owner|dom) \d{4}-\d{2}-\d{2}(?::[^{}\n]*)?\}/;
  /** Unpin (drop the line's core mark, keep the fact) or delete the whole line, saved like a hand edit. */
  async function dropCore(item: CoreMemory, mode: "unpin" | "delete") {
    if (coreBusy) return;
    coreBusy = true;
    try {
      const current = await getJSON<MemoryFile>(`/api/memory/file?file=${encodeURIComponent(item.file)}`);
      const lines = current.content.split("\n");
      // The exact line when the API gives it: two pinned lines can share their text and differ only in their tags.
      const at = item.raw !== undefined
        ? lines.indexOf(item.raw)
        : lines.findIndex((line) => PIN.test(line) && stripTags(line) === item.text);
      if (at < 0) {
        toast.show("error", "That line has changed since the list loaded. Reload the list and try again.");
        return;
      }
      if (mode === "unpin") lines[at] = lines[at].replace(PIN, "");
      else lines.splice(at, 1);
      await send("/api/memory/save", { file: item.file, version: current.version, content: lines.join("\n") });
      toast.show("ok", mode === "unpin" ? "Unpinned. The line stays in its file." : "Deleted the core memory.");
      core = await getJSON<CoreMemories>("/api/memory/core");
    } catch {
      // send() toasts the reason; a failed read shows on the next try
    } finally {
      coreBusy = false;
    }
  }
  const bullet = (text: string) => text.replace(/^[ \t]*[-*][ \t]+/, "");
  const index_chat = (source: string) => (/^-?\d+$/.test(source) ? (index?.chat_names[source] ?? `chat ${source}`) : "");

  let importOpen = $state(false); // the Import sheet

  /** Go somewhere else; a draft with changes asks first. */
  function guard(action: () => void) {
    if (dirty) leaving = action;
    else action();
  }

  async function save() {
    if (!file || saving) return;
    saving = true;
    saveError = "";
    try {
      const result = await send<{ message: string; content: string; version: string; entities?: Record<string, string> }>(
        "/api/memory/save",
        { file: file.file, version: file.version, content: draft },
        { quiet: true },
      );
      file = { ...file, content: result.content, version: result.version, entities: result.entities ?? file.entities };
      draft = result.content;
      toast.show("ok", result.message);
    } catch (error) {
      saveError = error instanceof ApiError ? error.message : "Couldn't save this file.";
    } finally {
      saving = false;
    }
  }

  // The soul conversation lives here, so closing the panel and coming back keeps it.
  let soulTurns = $state<SoulTurn[]>([]);

  // Waiting for you: imports and edits to review.
  let deciding = $state(false);
  let picked = $state<Record<number, boolean>>({}); // fact id -> unchecked; facts start checked
  let rejecting = $state<number | null>(null); // an import card waiting on its reject-all confirm

  async function decide<T extends MemoryWaiting & { message: string }>(path: string, body: object) {
    if (deciding) return;
    deciding = true;
    try {
      const result = await send<T>(path, body);
      waiting(result);
      toast.show("ok", result.message);
    } catch {
      // the toast says why
    } finally {
      deciding = false;
    }
  }

  const chosen = (card: MemoryWaiting["imports"][number]) => card.facts.filter((fact) => picked[fact.id] !== false).map((fact) => fact.id);

  // Import: a .txt or .md export, sent as base64 so the server applies the same document checks as Telegram.
  let service = $state("chatgpt");
  let exportFile = $state<File | null>(null);
  let importing = $state(false);
  let importError = $state("");
  let importResult = $state("");

  async function base64(blob: Blob): Promise<string> {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = "";
    for (let at = 0; at < bytes.length; at += 0x8000) binary += String.fromCharCode(...bytes.subarray(at, at + 0x8000));
    return btoa(binary);
  }

  async function runImport() {
    if (!exportFile || importing) return;
    importError = "";
    importResult = "";
    if (exportFile.size > MAX_DOCUMENT_BYTES) {
      importError = "Import documents must be 1 MB or smaller.";
      return;
    }
    importing = true;
    try {
      const result = await send<{ message: string } & MemoryWaiting>(
        "/api/memory/import",
        { service, filename: exportFile.name, data: await base64(exportFile) },
        { quiet: true },
      );
      importResult = result.message;
      waiting(result);
      exportFile = null;
    } catch (error) {
      importError = error instanceof ApiError ? error.message : "Couldn't read that file.";
    } finally {
      importing = false;
    }
  }

  async function copyPrompt() {
    try {
      await navigator.clipboard.writeText(index?.export_prompt ?? "");
      toast.show("ok", "Prompt copied.");
    } catch {
      toast.show("error", "Couldn't copy. Open “Show the prompt” and copy it from there.");
    }
  }

  const serviceLabel = (id: string) => index?.services.find((item) => item.id === id)?.label ?? id;
  const count = (n: number) => n.toLocaleString("en-US");
</script>

<PageHeader kicker="Destination" title="Memory" subtitle={destination.blurb}>
  {#snippet aside()}
    <a href="/api/memory/export" download class={buttonVariants({ variant: "outline" })}>
      <Download aria-hidden="true" /> Export memory
    </a>
    <Button variant="outline" onclick={() => (importOpen = true)}><Upload aria-hidden="true" /> Import memories</Button>
    <Button
      variant={talking ? "default" : "secondary"}
      aria-pressed={talking}
      onclick={() => (talking ? stopTalking() : talk(open === "memory/WORLD.md" ? "WORLD.md" : "SOUL.md"))}
    >
      <Sparkles aria-hidden="true" /> Talk to Dom about his soul and world
    </Button>
  {/snippet}
</PageHeader>
<PageTabs {destination} {tab} />

{#if index === null && failed}
  <StatusNote tone="error" title="Couldn't load memory" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="secondary" onclick={load}>Retry</Button>
  </StatusNote>
{:else if index === null}
  <p class="sr-only" role="status">Loading memory…</p>
  <div class="grid gap-4 lg:grid-cols-[17rem_minmax(0,1fr)]" aria-hidden="true">
    <div class="h-72 animate-pulse rounded-xl border bg-surface"></div>
    <div class="h-96 animate-pulse rounded-xl border bg-surface"></div>
  </div>
{:else}
  {@const memory = index}
  {#if memory.imports.length || memory.edits.length}
    <section aria-labelledby="waiting-title" class="mb-6">
      <h2 id="waiting-title" class="kicker mb-3">Waiting for you</h2>
      <div class="grid gap-4 lg:grid-cols-2">
        {#each memory.edits as edit (edit.id)}
          <Card.Root>
            <Card.Header>
              <Card.Title class="text-base">{edit.label}</Card.Title>
              <Card.Description class="break-words">{edit.title}</Card.Description>
            </Card.Header>
            <Card.Content class="grid gap-4">
              <DiffLines lines={edit.diff} language="markdown" />
              <div class="flex flex-wrap gap-2">
                <Button disabled={deciding} onclick={() => decide("/api/memory/proposal", { id: edit.id, decision: "approve" })}>Approve</Button>
                <Button variant="outline" disabled={deciding} onclick={() => decide("/api/memory/proposal", { id: edit.id, decision: "reject" })}>Reject</Button>
              </div>
            </Card.Content>
          </Card.Root>
        {/each}
        {#each memory.imports as card (card.id)}
          {@const ids = chosen(card)}
          <Card.Root>
            <Card.Header>
              <Card.Title class="text-base">
                {count(card.facts.length)} {card.facts.length === 1 ? "memory" : "memories"} imported from {card.service}
              </Card.Title>
              <Card.Description>Tick the ones to keep. Dom will organize them into your notes during his downtime.</Card.Description>
            </Card.Header>
            <Card.Content class="grid gap-4">
              <ul class="grid max-h-64 gap-2 overflow-y-auto">
                {#each card.facts as fact (fact.id)}
                  <li class="flex items-start gap-2.5">
                    <Checkbox
                      id={`fact-${fact.id}`}
                      class="mt-0.5"
                      checked={picked[fact.id] !== false}
                      onCheckedChange={(value) => (picked[fact.id] = value)}
                    />
                    <Label for={`fact-${fact.id}`} class="items-start text-sm leading-snug font-normal break-words">{fact.text}</Label>
                  </li>
                {/each}
              </ul>
              <div class="flex flex-wrap gap-2">
                <Button
                  disabled={deciding || ids.length === 0}
                  onclick={() => decide("/api/memory/import-review", { id: card.id, decision: "selected", selected: ids })}
                >
                  Approve selected
                </Button>
                <Button variant="secondary" disabled={deciding} onclick={() => decide("/api/memory/import-review", { id: card.id, decision: "all" })}>
                  Approve all
                </Button>
                <Button variant="destructive" disabled={deciding} onclick={() => (rejecting = card.id)}>Reject all</Button>
              </div>
            </Card.Content>
          </Card.Root>
        {/each}
      </div>
    </section>
  {/if}

  {#if talking}
    <SoulChat index={memory} bind:turns={soulTurns} file={talkAbout} back={fromWorld ? "World" : "Files"} onwaiting={waiting} onclose={stopTalking} />
  {:else}
  <div class="grid items-start gap-5 lg:grid-cols-[17rem_minmax(0,1fr)]">
    <!-- On a phone the file list is a screen of its own, and a file opens full width with a way back. -->
    <!-- On a wide screen the list and the open file each fit the viewport and scroll on their own, so a file deep in
         the list opens beside it without scrolling back up. -->
    <nav
      aria-label="Memory files"
      class={cn("rounded-xl border bg-surface pb-2 lg:sticky lg:top-4 lg:max-h-[calc(100dvh-2rem)] lg:overflow-y-auto lg:overscroll-contain", open !== null && "max-lg:hidden")}
    >
      <!-- The filter stays at the top of the list, and each group's heading sticks under it while its files scroll. -->
      <div class="sticky top-0 z-10 rounded-t-xl bg-surface px-2 pt-2 pb-1.5">
        <label for="memory-filter" class="sr-only">Filter memory files</label>
        <div class="relative">
          <Search class="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <input
            id="memory-filter"
            type="search"
            bind:value={filter}
            onkeydown={(event) => event.key === "Escape" && filter && (event.preventDefault(), (filter = ""))}
            placeholder="Filter files"
            autocomplete="off"
            class="h-9 w-full rounded-lg border border-input bg-canvas/50 pr-2.5 pl-8 text-sm outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
          />
        </div>
        <p class="sr-only" role="status">{needle ? `${shownCount} ${shownCount === 1 ? "file matches" : "files match"}` : ""}</p>
      </div>
      {#if !needle || "core memories".includes(needle)}
        <ul class="grid px-2 pb-1">
          <li>
            <button
              type="button"
              aria-current={open === CORE ? "page" : undefined}
              onclick={() => guard(openCore)}
              class={cn(row, open === CORE && rowHere)}
            >
              <Pin class="size-4 shrink-0" aria-hidden="true" />
              <span class="min-w-0 truncate">Core memories</span>
            </button>
          </li>
        </ul>
      {/if}
      {#snippet entries(files: { path: string; label: string }[], labelledby: string)}
        <ul class="grid px-2" aria-labelledby={labelledby}>
          {#each files as item (item.path)}
            {@const here = open === item.path}
            <li>
              <button
                type="button"
                aria-current={here ? "page" : undefined}
                title={item.label.length > 28 ? item.label : undefined}
                onclick={() => guard(() => openFile(item.path))}
                class={cn(row, here && rowHere)}
              >
                {#if archived.has(item.path)}<Archive class="size-4 shrink-0" aria-hidden="true" />{:else if item.path.startsWith("private/")}<Lock class="size-4 shrink-0" aria-hidden="true" />{:else}<FileText class="size-4 shrink-0" aria-hidden="true" />{/if}
                <span class="min-w-0 truncate">{item.label}</span>
              </button>
            </li>
          {/each}
        </ul>
      {/snippet}
      {#each sections as section, at (section.heading)}
        {@const id = `memory-group-${at}`}
        {#if section.archive}
          <!-- The record: journal months and former lines, read-only and folded until wanted (open while filtering). -->
          <details class="group mt-1 border-t pt-1" open={!!needle || section.files.some((item) => item.path === open)}>
            <summary class="mx-2 flex min-h-9 cursor-pointer list-none items-center gap-1.5 rounded-[0.6rem] px-2 outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
              <ChevronRight class="size-3.5 text-muted-foreground transition-transform duration-(--motion-quick) group-open:rotate-90" aria-hidden="true" />
              <h2 {id} class="kicker">{section.heading}</h2>
              <span class="ml-auto text-caption text-muted-foreground tabular-nums">{section.files.length}</span>
            </summary>
            {@render entries(section.files, id)}
          </details>
        {:else}
          <h2 {id} class="sticky top-[3.125rem] z-[5] flex items-center gap-2 border-b border-line/60 bg-surface/95 px-4 pt-2.5 pb-1.5 backdrop-blur-sm">
            <span class="kicker">{section.heading}</span>
            <span class="ml-auto text-caption text-muted-foreground tabular-nums">{section.files.length}</span>
          </h2>
          <div class="pt-1 pb-1.5">{@render entries(section.files, id)}</div>
        {/if}
      {/each}
      {#if needle && shownCount === 0 && !"core memories".includes(needle)}
        <p class="px-4 py-3 text-meta text-muted-foreground">No files match “{filter.trim()}”.</p>
      {/if}
    </nav>

    <div
      bind:this={pane}
      class={cn("min-w-0 scroll-mt-4 lg:sticky lg:top-4 lg:flex lg:max-h-[calc(100dvh-2rem)] lg:flex-col lg:*:min-h-0", open === null && "max-lg:hidden")}
    >
      {#if open === CORE}
        <Card.Root aria-labelledby="core-title">
          <Card.Header>
            <Button variant="ghost" size="sm" class="mb-1 w-fit lg:hidden" onclick={() => (open = null)}>
              <ArrowLeft aria-hidden="true" /> Files
            </Button>
            <div class="flex flex-wrap items-center gap-1.5">
              <h2 id="core-title" class="text-lg font-semibold">Core memories</h2>
              <InfoGlyph label="About core memories">
                Lines that never fade: summaries never drop them and the memory pass never rewrites them. Dom pins his own with a reason; lines you add or change
                on this page are pinned for you. Unpin one to let it age like the rest, or delete the line.
              </InfoGlyph>
              {#if core}<span class="ml-auto text-meta text-muted-foreground tabular-nums">{core.items.length} of {core.cap}</span>{/if}
            </div>
          </Card.Header>
          <Card.Content class="lg:overflow-y-auto">
            {#if coreError}
              <StatusNote tone="error" title="Couldn't load core memories" detail={coreError}>
                <Button variant="secondary" onclick={openCore}>Retry</Button>
              </StatusNote>
            {:else if core === null}
              <div class="h-40 animate-pulse rounded-lg bg-raised/60" aria-hidden="true"></div>
            {:else if core.items.length === 0}
              <StatusNote title="No core memories yet" detail="Dom pins the moments that matter to him; lines you write here are pinned too." />
            {:else}
              <ul class="divide-y divide-line/70 rounded-lg border" aria-label="Core memories">
                {#each core.items as item, index (index)}
                  <li class="flex items-start gap-3 px-3 py-2.5">
                    <span
                      class={cn("mt-0.5 grid size-7 shrink-0 place-items-center rounded-full border", item.by === "owner" ? "border-signal/50 bg-tint text-signal-strong" : "border-status-held/50 text-status-held")}
                      aria-hidden="true"
                    >
                      {#if item.by === "owner"}<UserRound class="size-3.5" />{:else}<Bot class="size-3.5" />{/if}
                    </span>
                    <div class="min-w-0 flex-1">
                      <p class="text-body break-words">{bullet(item.text)}</p>
                      <p class="mt-0.5 text-meta text-muted-foreground">
                        <span class={item.by === "owner" ? "text-signal-strong" : "text-status-held"}>{item.by === "owner" ? "Pinned by you" : "Pinned by Dom"}</span>
                        on {item.day} in {labelOf(item.file)}{item.source !== "any" && index_chat(item.source) ? `, from ${index_chat(item.source)}` : ""}
                      </p>
                      {#if item.why}<p class="mt-1 text-meta break-words text-muted-foreground">Why: {item.why}</p>{/if}
                    </div>
                    <IconButton
                      label={`Delete core memory: ${bullet(item.text)}`}
                      icon={Trash}
                      class="bg-transparent text-muted-foreground hover:bg-coral-tint hover:text-coral"
                      disabled={coreBusy}
                      onclick={() => (dropping = item)}
                    />
                  </li>
                {/each}
              </ul>
            {/if}
          </Card.Content>
        </Card.Root>
      {:else if fileError}
        <StatusNote tone="error" title="Couldn't open that file" detail={fileError}>
          <Button variant="secondary" onclick={() => open && openFile(open)}>Retry</Button>
        </StatusNote>
      {:else if file}
        {@const locked = file.readonly}
        <Card.Root>
          <Card.Header>
            <Button variant="ghost" size="sm" class="mb-1 w-fit lg:hidden" onclick={() => guard(() => (open = null))}>
              <ArrowLeft aria-hidden="true" /> Files
            </Button>
            <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
              <Card.Title class="text-lg">{file.label}</Card.Title>
              {#if archived.has(file.file)}
                <span class="rounded-full border px-2 text-caption text-muted-foreground">Archive, read-only</span>
              {:else if file.readonly}
                <InfoGlyph label="About this file">Dom writes this from message counts, so it is read-only.</InfoGlyph>
              {:else if file.file.startsWith("private/")}
                <InfoGlyph label="About private files">Only in your DM: Dom never shares this file with other chats.</InfoGlyph>
              {/if}
            </div>
            <p class="font-mono text-caption text-muted-foreground">{file.file}</p>
            {#if file.file === SOUL || file.file === "memory/WORLD.md"}
              {@const about = file.file === SOUL ? "SOUL.md" : "WORLD.md"}
              <Card.Action>
                <Button variant="secondary" size="sm" onclick={() => talk(about)}>
                  <Sparkles aria-hidden="true" /> Talk it over with Dom
                </Button>
              </Card.Action>
            {/if}
          </Card.Header>
          <Card.Content class="grid gap-3 lg:overflow-y-auto">
            <Tabs.Root bind:value={view}>
              <Tabs.List class="mb-1 w-fit">
                <Tabs.Trigger value="preview" class="px-3"><Eye aria-hidden="true" /> Preview</Tabs.Trigger>
                <Tabs.Trigger value="edit" class="px-3"><PenLine aria-hidden="true" /> {locked ? "Source" : "Edit"}</Tabs.Trigger>
              </Tabs.List>
              <Tabs.Content value="preview">
                {#if draft.trim()}
                  <div class="min-h-[22rem] rounded-[0.65rem] border bg-canvas/50 px-4 py-3.5 sm:px-5 lg:min-h-[28rem]">
                    <Markdown text={draft} breaks={journal(file.file)} />
                  </div>
                {:else}
                  <StatusNote title="Nothing here yet" detail={locked ? "Dom fills this in as he goes." : "Open Edit to write the first lines."} />
                {/if}
                {#if dirty}<p class="mt-2 text-sm text-muted-foreground">Showing your unsaved edits.</p>{/if}
              </Tabs.Content>
              <Tabs.Content value="edit" class="grid gap-3">
                <Label for="memory-text" class="sr-only">{file.label}</Label>
                <CodeEditor
                  id="memory-text"
                  class={cn(field, "min-h-[22rem] resize-y leading-relaxed lg:min-h-[28rem]")}
                  language={file.file}
                  spellcheck="false"
                  readonly={locked}
                  aria-describedby="memory-count"
                  aria-invalid={over || undefined}
                  bind:value={draft}
                />
                <p id="memory-count" class={cn("text-sm text-muted-foreground", over && "text-coral")}>
                  {count(used)}
                  {#if file.limit != null}of {count(file.limit)} characters{:else}characters, no limit{/if}{over ? ". Over the limit: shorten it to save." : ""}
                </p>
              </Tabs.Content>
            </Tabs.Root>
            {#if saveError}
              <div role="alert" class="rounded-lg border border-coral/45 bg-coral-tint px-3.5 py-3 text-sm text-coral">
                <p>{saveError}</p>
                {#if saveError.includes("changed since")}
                  <Button variant="outline" size="sm" class="mt-2" onclick={() => file && openFile(file.file)}>Discard my edit and reload</Button>
                {/if}
              </div>
            {/if}
          </Card.Content>
          {#if !locked && (view === "edit" || dirty)}
            <!-- Sticky on a phone, so Save stays in reach however long the file is. -->
            <Card.Footer class="sticky bottom-0 z-10 flex flex-wrap gap-2 border-t bg-card py-3 lg:static">
              <Button disabled={!dirty || over || saving} onclick={save}>{saving ? "Saving…" : "Save changes"}</Button>
              <Button variant="outline" disabled={!dirty || saving} onclick={() => ((draft = file?.content ?? ""), (saveError = ""))}>Discard</Button>
            </Card.Footer>
          {/if}
        </Card.Root>
      {:else}
        <p class="sr-only" role="status">Loading the file…</p>
        <div class="h-96 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
      {/if}
    </div>
  </div>

  {/if}

  <!-- Import: a .txt or .md export from another assistant; its facts wait above for review. -->
  <Sheet.Root bind:open={importOpen}>
    <Sheet.Content class="w-[min(30rem,100vw)] overflow-y-auto bg-overlay" onswipe={() => (importOpen = false)}>
      <div class="sticky top-0 z-10 flex items-center gap-2 border-b bg-overlay px-4 py-3">
        <Sheet.Title class="min-w-0 flex-1 text-title font-semibold">Import memories</Sheet.Title>
        <Sheet.Close>
          {#snippet child({ props })}<IconButton {...props} label="Close import" icon={X} />{/snippet}
        </Sheet.Close>
      </div>
      <Sheet.Description class="px-4 pt-3 text-body text-muted-foreground">Bring in what another assistant knows about you. Its facts wait on this page for your review.</Sheet.Description>
      <div class="grid gap-5 px-4 py-4">
        <div class="grid gap-2">
          <p class="text-sm">Paste this prompt into ChatGPT, Claude, Muse or any other assistant, and save its answer as a .txt or .md file.</p>
          <div class="flex flex-wrap items-center gap-2">
            <Button variant="secondary" onclick={copyPrompt}><Copy aria-hidden="true" /> Copy prompt</Button>
          </div>
          <details class="rounded-lg border bg-canvas/50 px-3 py-2 text-sm">
            <summary class="cursor-pointer font-medium">Show the prompt</summary>
            <pre class="mt-2 max-h-64 overflow-auto font-mono text-[0.8rem] leading-relaxed break-words whitespace-pre-wrap">{memory.export_prompt}</pre>
          </details>
        </div>
        <p class="text-sm">Then upload that file here.</p>
        <form class="grid gap-4" onsubmit={(event) => (event.preventDefault(), runImport())}>
          <div class="grid gap-1.5">
            <Label id="import-service-label">Service</Label>
            <Select.Root type="single" bind:value={service}>
              <Select.Trigger class="h-9 w-full" aria-labelledby="import-service-label">{serviceLabel(service)}</Select.Trigger>
              <Select.Content>
                {#each memory.services as item (item.id)}
                  <Select.Item value={item.id} label={item.label} />
                {/each}
              </Select.Content>
            </Select.Root>
          </div>
          <div class="grid gap-1.5">
            <Label for="import-file">Export file (.txt or .md)</Label>
            <Input
              id="import-file"
              type="file"
              accept=".txt,.md,text/plain,text/markdown"
              onchange={(event) => (exportFile = event.currentTarget.files?.[0] ?? null)}
            />
          </div>
          <Button type="submit" class="w-fit" disabled={!exportFile || importing}><Upload aria-hidden="true" /> {importing ? "Importing…" : "Import"}</Button>
        </form>
        {#if importError}<p role="alert" class="text-sm text-coral">{importError}</p>{/if}
        {#if importResult}<p role="status" class="text-sm text-muted-foreground">{importResult}</p>{/if}
      </div>
    </Sheet.Content>
  </Sheet.Root>
{/if}

<AlertDialog.Root open={leaving !== null} onOpenChange={(value) => !value && (leaving = null)}>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Discard your changes?</AlertDialog.Title>
      <AlertDialog.Description>{file?.label} has edits you haven't saved. Leaving drops them.</AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">Keep editing</AlertDialog.Cancel>
      <AlertDialog.Action variant="destructive-solid" onclick={() => { const go = leaving; leaving = null; go?.(); }}>Discard changes</AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<AlertDialog.Root open={rejecting !== null} onOpenChange={(value) => !value && (rejecting = null)}>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Reject this whole import?</AlertDialog.Title>
      <AlertDialog.Description>Dom drops every fact still waiting in it. You can import the file again later.</AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">Keep them</AlertDialog.Cancel>
      <AlertDialog.Action
        variant="destructive-solid"
        onclick={() => { const id = rejecting; rejecting = null; id !== null && decide("/api/memory/import-review", { id, decision: "reject" }); }}
      >
        Reject all
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<AlertDialog.Root open={dropping !== null} onOpenChange={(value) => !value && (dropping = null)}>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Delete this core memory?</AlertDialog.Title>
      <AlertDialog.Description>
        “{dropping ? bullet(dropping.text) : ""}”. Unpin it to keep the line and let it age like the rest, or delete the line from {dropping ? labelOf(dropping.file) : ""}.
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">Keep it</AlertDialog.Cancel>
      <AlertDialog.Action variant="secondary" onclick={() => { const item = dropping; dropping = null; item && dropCore(item, "unpin"); }}>Unpin, keep the line</AlertDialog.Action>
      <AlertDialog.Action variant="destructive-solid" onclick={() => { const item = dropping; dropping = null; item && dropCore(item, "delete"); }}>Delete the line</AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>
