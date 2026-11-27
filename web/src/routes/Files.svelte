<script lang="ts">
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import Download from "@lucide/svelte/icons/download";
  import FileIcon from "@lucide/svelte/icons/file";
  import Folder from "@lucide/svelte/icons/folder";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import Upload from "@lucide/svelte/icons/upload";
  import { onMount } from "svelte";
  import { getJSON, send } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import Confirm from "$lib/components/work/Confirm.svelte";
  import type { Destination, Tab } from "$lib/nav";
  import { toast } from "$lib/toast.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  // GET /api/files/list, /preview (dom/web/api_files.py).
  type Area = "workspace" | "home";
  type Entry = { name: string; kind: "dir" | "file"; size: number | null; modified: string };
  type Listing = { area: Area; path: string; writable: boolean; entries: Entry[]; truncated: boolean };
  type Preview =
    | { kind: "text"; name: string; text: string; offset: number; next_offset: number | null }
    | { kind: "image" | "none"; name: string; size: number; modified: string };

  const MAX_UPLOAD = 20_000_000;
  const AREAS: { id: Area; label: string; blurb: string }[] = [
    { id: "workspace", label: "Workspace", blurb: "Shared with Dom and his desktop (it is /workspace there)." },
    { id: "home", label: "Dom's files", blurb: "What Dom may read: memory, media, briefs and skills. Read-only." },
  ];

  let area = $state<Area>("workspace");
  let path = $state("");
  let listing = $state<Listing | null>(null);
  let failed = $state("");
  let selected = $state<string | null>(null); // a file's path, for the preview
  let preview = $state<Preview | null>(null);
  let previewFailed = $state("");
  let deleting = $state<string | null>(null);
  let busy = $state(false);
  let input = $state<HTMLInputElement | null>(null);

  const query = (rel: string, extra = "") => `area=${area}&path=${encodeURIComponent(rel)}${extra}`;
  const raw = (rel: string, download = false) => `/api/files/raw?${query(rel, download ? "&download=true" : "")}`;
  const join = (name: string) => (path ? `${path}/${name}` : name);
  const crumbs = $derived(path ? path.split("/") : []);
  const size = (bytes: number | null) =>
    bytes === null ? "" : bytes < 1000 ? `${bytes} B` : bytes < 1_000_000 ? `${Math.round(bytes / 1000)} KB` : `${(bytes / 1_000_000).toFixed(1)} MB`;
  const when = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

  async function load(next: string = path) {
    try {
      listing = await getJSON<Listing>(`/api/files/list?${query(next)}`);
      path = next;
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  function switchArea(next: Area) {
    area = next;
    path = "";
    listing = null;
    selected = null;
    preview = null;
    load("");
  }

  function open(entry: Entry) {
    if (entry.kind === "dir") {
      selected = null;
      preview = null;
      load(join(entry.name));
    } else {
      show(join(entry.name));
    }
  }

  async function show(rel: string, offset = 0) {
    selected = rel;
    previewFailed = "";
    if (offset === 0) preview = null;
    try {
      const next = await getJSON<Preview>(`/api/files/preview?${query(rel, offset ? `&offset=${offset}` : "")}`);
      preview = offset && preview?.kind === "text" && next.kind === "text" ? { ...next, text: preview.text + next.text, offset: 0 } : next;
    } catch (error) {
      previewFailed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  // The file as base64 for the JSON upload, without building one huge string argument.
  async function encode(file: File): Promise<string> {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let at = 0; at < bytes.length; at += 0x8000) binary += String.fromCharCode(...bytes.subarray(at, at + 0x8000));
    return btoa(binary);
  }

  async function upload(event: Event) {
    const files = Array.from((event.currentTarget as HTMLInputElement).files ?? []);
    if (input) input.value = "";
    busy = true;
    try {
      for (const file of files) {
        if (file.size > MAX_UPLOAD) {
          toast.show("error", `${file.name} is larger than 20 MB.`);
          continue;
        }
        try {
          const done = await send<{ message: string }>("/api/files/upload", { folder: path, name: file.name, data: await encode(file) });
          toast.show("ok", done.message);
        } catch {
          // send() already showed why
        }
      }
    } finally {
      busy = false;
    }
    await load();
  }

  async function remove(rel: string) {
    busy = true;
    try {
      const done = await send<{ message: string }>("/api/files/delete", { path: rel });
      toast.show("ok", done.message);
      if (selected === rel) {
        selected = null;
        preview = null;
      }
    } catch {
      // send() already showed why
    } finally {
      busy = false;
    }
    await load();
  }

  onMount(() => load(""));
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

<div class="mb-4 flex flex-wrap items-center gap-2" role="group" aria-label="Which files">
  {#each AREAS as item (item.id)}
    <Button variant={area === item.id ? "secondary" : "outline"} size="sm" aria-pressed={area === item.id} onclick={() => area !== item.id && switchArea(item.id)}>
      {item.label}
    </Button>
  {/each}
  <p class="basis-full text-sm text-muted-foreground sm:ml-2 sm:basis-auto">{AREAS.find((item) => item.id === area)?.blurb}</p>
</div>

{#if listing === null && failed}
  <StatusNote tone="error" title="Couldn't load the files" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="outline" onclick={() => load()}>Try again</Button>
  </StatusNote>
{:else if listing === null}
  <div class="h-40 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  <div class="mb-3 flex flex-wrap items-center gap-2">
    <nav aria-label="Folder" class="min-w-0 flex-1">
      <ol class="flex flex-wrap items-center gap-1 text-sm">
        <li>
          <button type="button" class="rounded px-1 font-medium underline-offset-4 hover:underline focus-visible:ring-3" onclick={() => load("")}>
            {AREAS.find((item) => item.id === area)?.label}
          </button>
        </li>
        {#each crumbs as crumb, index (index)}
          <li class="flex items-center gap-1">
            <ChevronRight class="size-3.5 text-muted-foreground" aria-hidden="true" />
            <button type="button" class="rounded px-1 underline-offset-4 hover:underline focus-visible:ring-3" onclick={() => load(crumbs.slice(0, index + 1).join("/"))}>{crumb}</button>
          </li>
        {/each}
      </ol>
    </nav>
    {#if listing.writable}
      <input bind:this={input} type="file" multiple hidden aria-label="Choose files to upload" onchange={upload} />
      <Button variant="outline" size="sm" disabled={busy} onclick={() => input?.click()}>
        <Upload aria-hidden="true" /> Upload here
      </Button>
    {/if}
  </div>

  {#if listing.entries.length === 0}
    <StatusNote
      title={path ? "This folder is empty" : area === "workspace" ? "Nothing in the workspace yet" : "Nothing to show yet"}
      detail={area === "workspace"
        ? "Documents you send Dom in your DM land in inbox/, and files he or his desktop saves appear here."
        : "Notes, kept media, briefs and skills show up here as Dom makes them."}
    />
  {:else}
    <ul class="grid gap-2" aria-label="Files and folders">
      {#each listing.entries as entry, index (entry.name)}
        {@const rel = join(entry.name)}
        <li>
          <Card.Root class={selected === rel ? "border-signal/45" : ""}>
            <Card.Content class="flex items-center gap-3 py-2.5">
              <button
                type="button"
                class="flex min-w-0 flex-1 items-center gap-3 rounded-lg text-left focus-visible:ring-3"
                aria-label={entry.kind === "dir" ? `${entry.name}, folder` : entry.name}
                aria-describedby={`file-meta-${index}`}
                onclick={() => open(entry)}
              >
                <span class="grid size-8 shrink-0 place-items-center rounded-lg border bg-raised text-signal" aria-hidden="true">
                  {#if entry.kind === "dir"}<Folder class="size-4" />{:else}<FileIcon class="size-4" />{/if}
                </span>
                <span class="min-w-0">
                  <span class="block truncate font-medium">{entry.name}{entry.kind === "dir" ? "/" : ""}</span>
                  <span id={`file-meta-${index}`} class="block text-sm text-muted-foreground">{entry.kind === "file" ? `${size(entry.size)} · ` : ""}{when(entry.modified)}</span>
                </span>
              </button>
              {#if entry.kind === "file"}
                <IconButton label={`Download ${entry.name}`} icon={Download} variant="outline" href={raw(rel, true)} download={entry.name} />
              {/if}
              {#if listing.writable}
                <IconButton label={`Delete ${entry.name}`} icon={Trash2} variant="destructive" disabled={busy} onclick={() => (deleting = rel)} />
              {/if}
            </Card.Content>
          </Card.Root>
        </li>
      {/each}
    </ul>
    {#if listing.truncated}
      <p class="mt-2 text-sm text-muted-foreground" role="status">Only the first {listing.entries.length} are shown. Open a folder to see the rest.</p>
    {/if}
  {/if}

  {#if selected !== null}
    <section class="mt-6" aria-label={`Preview of ${selected}`} aria-live="polite">
      <Card.Root>
        <Card.Header>
          <Card.Title class="break-all text-base">{selected.split("/").pop()}</Card.Title>
          {#if preview}
            <Card.Description>
              {#if preview.kind !== "text"}{size(preview.size)} · {when(preview.modified)}{:else}Text preview{/if}
            </Card.Description>
          {/if}
        </Card.Header>
        <Card.Content>
          {#if previewFailed}
            <p class="text-sm text-coral" role="alert">Couldn't open this file: {previewFailed}.</p>
          {:else if preview === null}
            <div class="h-24 animate-pulse rounded-lg bg-raised" aria-hidden="true"></div>
          {:else if preview.kind === "image"}
            <img src={raw(selected)} alt={`Preview of ${preview.name}`} class="max-h-[32rem] max-w-full rounded-lg border bg-raised object-contain" />
          {:else if preview.kind === "text"}
            <!-- Focusable so a keyboard can scroll a long text. -->
            <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
            <pre class="max-h-[32rem] overflow-auto rounded-lg border bg-canvas/50 p-3 text-sm break-words whitespace-pre-wrap" tabindex="0">{preview.text}</pre>
            {#if preview.next_offset !== null}
              <Button class="mt-3" variant="outline" size="sm" onclick={() => selected && preview?.kind === "text" && show(selected, preview.next_offset ?? 0)}>Show more</Button>
            {/if}
          {:else}
            <p class="text-sm text-muted-foreground">No preview for this kind of file. Download it to open it.</p>
          {/if}
        </Card.Content>
      </Card.Root>
    </section>
  {/if}
{/if}

<Confirm
  bind:open={() => deleting !== null, (open) => !open && (deleting = null)}
  title={`Delete ${deleting?.split("/").pop()}?`}
  description="It is removed from the workspace for good, for Dom and his desktop too. A folder can only be deleted when it is empty."
  keep="Keep it"
  action="Delete"
  onconfirm={() => deleting && remove(deleting)}
/>
