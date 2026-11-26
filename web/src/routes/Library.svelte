<script lang="ts">
  import Download from "@lucide/svelte/icons/download";
  import FileText from "@lucide/svelte/icons/file-text";
  import Image from "@lucide/svelte/icons/image";
  import Mic from "@lucide/svelte/icons/mic";
  import Video from "@lucide/svelte/icons/video";
  import { onMount } from "svelte";
  import { getJSON, type BriefsData, type MediaFile, type MediaFiles, type MediaPart, type MediaUsage, type OwnData } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import StripList from "$lib/components/kit/StripList.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Progress } from "$lib/components/ui/progress";
  import type { Destination, Tab } from "$lib/nav";

  // R9: what Dom made and kept, in one place. Media comes from the owner-only /api/media/files (same origin, so the
  // CSP's img-src and media-src 'self' allow it); the rest links to where it already lives.
  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  let files = $state<MediaFile[]>([]);
  let next = $state<string | null>(null);
  let usage = $state<MediaUsage | null>(null);
  let own = $state<OwnData | null>(null);
  let briefs = $state<BriefsData | null>(null);
  let failed = $state("");
  let loaded = $state(false);
  let selected = $state<string | null>(null);

  async function load() {
    try {
      const page = await getJSON<MediaFiles>("/api/media/files");
      files = page.files;
      next = page.next;
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
    loaded = true;
  }

  async function more() {
    if (!next) return;
    const page = await getJSON<MediaFiles>(`/api/media/files?before=${encodeURIComponent(next)}`);
    files = [...files, ...page.files];
    next = page.next;
  }

  onMount(() => {
    load();
    // The side lists are extras: each failing quietly leaves its section out.
    getJSON<MediaUsage>("/api/media/usage").then((value) => (usage = value), () => (usage = null));
    getJSON<OwnData>("/api/work/own").then((value) => (own = value), () => (own = null));
    getJSON<BriefsData>("/api/work/briefs").then((value) => (briefs = value), () => (briefs = null));
  });

  const KIND: Record<string, { label: string; icon: typeof Image }> = {
    photo: { label: "Photo", icon: Image },
    voice: { label: "Voice note", icon: Mic },
    video_note: { label: "Video note", icon: Video },
  };
  const kindOf = (file: MediaFile) => KIND[file.kind] ?? { label: file.kind, icon: FileText };
  const label = (file: MediaFile) => `${file.special ? "Special " : ""}${kindOf(file).label.toLowerCase()}`.replace(/^./, (c) => c.toUpperCase());
  const key = (file: MediaFile) => `${file.chat_id}:${file.msg_id}`;

  function size(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  }
  const money = (cost: number) => `$${cost.toFixed(cost < 0.1 ? 3 : 2)}`;
  // What a generation cost: free when the local studio made it; a cost not reported yet (OpenRouter can be late) says so.
  const spend = (part: Pick<MediaPart, "cost" | "pending" | "free">) =>
    part.free ? "local, free" : part.pending ? (part.cost > 0 ? `${money(part.cost)} so far, more pending` : "cost pending") : money(part.cost);
  const source = (part: MediaPart) => (part.source === "local" ? "Local studio" : `Cloud backup${part.host ? ` · ${part.host}` : ""}`);
  const lasted = (seconds: number) => (seconds < 60 ? `${seconds.toFixed(1)} s` : `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`);
  const when = (at: string) => new Date(at).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
  const taskFiles = $derived((own?.tasks ?? []).filter((task) => task.files.length));
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#if usage}
  <section aria-labelledby="library-disk" class="mb-5 rounded-xl border bg-surface p-4">
    <div class="flex flex-wrap items-baseline justify-between gap-2">
      <h2 id="library-disk" class="flex items-center gap-1 font-medium">
        Disk use
        <InfoGlyph label="About disk use">
          Dom keeps what he sends up to this size. Past it, the oldest files leave the disk; Telegram still has them, so
          they open from there.
        </InfoGlyph>
      </h2>
      <p class="text-sm text-muted-foreground">
        <span class="font-mono">{size(usage.bytes)}</span>{#if usage.cap_bytes > 0}{" "}of <span class="font-mono">{size(usage.cap_bytes)}</span>{/if}
        · {usage.files} {usage.files === 1 ? "file" : "files"}
      </p>
    </div>
    {#if usage.cap_bytes > 0}
      <Progress class="mt-3" value={Math.min(usage.bytes, usage.cap_bytes)} max={usage.cap_bytes} aria-label="Disk use against the cap" />
    {:else}
      <p class="mt-2 text-sm text-muted-foreground">Keeping is off: the cap is 0, so new media stays only in Telegram.</p>
    {/if}
  </section>
{/if}

<section aria-labelledby="library-media" class="mb-8">
  <h2 id="library-media" class="mb-3 text-lg font-semibold">Media Dom sent</h2>
  {#if !loaded}
    <p class="sr-only" role="status">Loading the library…</p>
    <div class="h-56 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
  {:else if failed && files.length === 0}
    <StatusNote tone="error" title="Couldn't load the library" detail={`Dom's dashboard answered: ${failed}.`}>
      <Button variant="outline" onclick={load}>Try again</Button>
    </StatusNote>
  {:else if files.length === 0}
    <StatusNote title="Nothing kept yet" detail="Photos, voice notes and video notes Dom sends show here." />
  {:else}
    <StripList
      items={files}
      {key}
      strip={(file) => ({ status: file.stored ? "done" : "queued", title: file.text || label(file), at: file.at, meta: `${label(file)} · ${file.chat}` })}
      detailTitle={(file) => `${label(file)} in ${file.chat}`}
      label="Media Dom sent, newest first"
      name="Media"
      search="Filter media"
      bind:selected
      hasMore={next !== null}
      onmore={more}
      contained={files.length > 12}
      level={3}
    >
      {#snippet actions(file)}
        {#if file.stored && file.kind === "photo"}
          <img src={file.url} alt="" loading="lazy" class="mr-2 size-9 rounded-md border object-cover" />
        {:else}
          {@const meta = kindOf(file)}
          <span class="mr-2 grid size-9 place-items-center rounded-md border bg-raised text-muted-foreground" aria-hidden="true">
            <meta.icon class="size-4" />
          </span>
        {/if}
      {/snippet}
      {#snippet details(file)}
        <div class="grid gap-4">
          {#if file.kind === "photo"}
            <img src={file.url} alt={file.text || "A photo Dom sent"} class="max-h-96 w-full rounded-lg border bg-raised object-contain" />
          {:else if file.kind === "voice"}
            <audio controls preload="none" src={file.url} class="w-full" aria-label={`Voice note in ${file.chat}`}></audio>
          {:else if file.kind === "video_note"}
            <!-- svelte-ignore a11y_media_has_caption -->
            <video controls preload="metadata" src={file.url} class="aspect-square max-h-80 rounded-full border bg-raised object-cover" aria-label={`Video note in ${file.chat}`}></video>
          {/if}
          <dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-body">
            {#if file.text}<dt class="text-muted-foreground">Scene</dt><dd class="break-words">{file.text}</dd>{/if}
            <dt class="text-muted-foreground">Chat</dt><dd>{file.chat}</dd>
            <dt class="text-muted-foreground">Sent</dt><dd>{when(file.at)}</dd>
            {#if file.bytes}<dt class="text-muted-foreground">Size</dt><dd class="font-mono">{size(file.bytes)}</dd>{/if}
          </dl>
          {#if file.made}
            {@const made = file.made}
            <dl aria-label="How it was made" class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 border-t pt-3 text-body">
              <dt class="text-muted-foreground">Made by</dt><dd>{source(made)}</dd>
              <dt class="text-muted-foreground">Model</dt><dd class="font-mono break-all">{made.model}</dd>
              {#if made.outfit}<dt class="text-muted-foreground">Outfit</dt><dd>{made.outfit}</dd>{/if}
              {#if made.preset}<dt class="text-muted-foreground">Preset</dt><dd>{made.preset}</dd>{/if}
              {#if made.duration}<dt class="text-muted-foreground">Length</dt><dd>{lasted(made.duration)}</dd>{/if}
              {#if made.seconds}<dt class="text-muted-foreground">Took</dt><dd>{lasted(made.seconds)}</dd>{/if}
              {#if made.redo}
                <dt class="text-muted-foreground">Redone</dt>
                <dd>Once, after looking; the {made.redo.kept} try was sent · {spend(made.redo)}</dd>
              {/if}
              {#if made.voice}
                <dt class="text-muted-foreground">Voice</dt>
                <dd><span class="font-mono break-all">{made.voice.model}</span> · {source(made.voice)} · {spend(made.voice)}</dd>
              {/if}
              {#if made.still}
                <dt class="text-muted-foreground">Still</dt>
                <dd>
                  <span class="font-mono break-all">{made.still.model}</span> · {source(made.still)} · {spend(made.still)}
                  {#if made.still.redo} · redone, the {made.still.redo.kept} try was used · {spend(made.still.redo)}{/if}
                  {#if file.still_url}· <a href={file.still_url} target="_blank" rel="noopener" class="font-medium text-signal-strong underline underline-offset-2">open</a>{/if}
                </dd>
              {/if}
              <dt class="text-muted-foreground">Cost</dt><dd class="font-medium">{spend(made)}</dd>
            </dl>
          {/if}
          {#if !file.stored}
            <p class="text-sm text-muted-foreground">No longer on disk: it opens from Telegram.</p>
          {/if}
          <div>
            <Button variant="outline" size="sm" href={file.url} download>
              <Download data-icon="inline-start" aria-hidden="true" /> Download
            </Button>
          </div>
        </div>
      {/snippet}
    </StripList>
  {/if}
</section>

<div class="grid gap-4 md:grid-cols-2">
  <Card.Root aria-labelledby="library-task-files">
    <Card.Header>
      <Card.Title id="library-task-files" class="text-lg">Task files and screenshots</Card.Title>
    </Card.Header>
    <Card.Content>
      {#if taskFiles.length}
        <ul class="grid gap-3">
          {#each taskFiles as task (task.id)}
            <li>
              <p class="text-sm font-medium">{task.goal}</p>
              <ul class="mt-1 flex flex-wrap gap-2">
                {#each task.files as name (name)}
                  {@const url = `/api/work/own/${task.id}/files/${encodeURIComponent(name)}`}
                  <li>
                    <a href={url} target="_blank" rel="noopener" class="flex items-center gap-2 rounded-lg border bg-raised/40 p-1.5 pr-3 text-sm hover:bg-raised">
                      {#if /\.(png|jpe?g|webp)$/i.test(name)}
                        <img src={url} alt="" loading="lazy" class="size-9 rounded-md object-cover" />
                      {:else}
                        <FileText class="size-4 text-muted-foreground" aria-hidden="true" />
                      {/if}
                      <span class="font-mono break-all">{name}</span>
                    </a>
                  </li>
                {/each}
              </ul>
            </li>
          {/each}
        </ul>
      {:else}
        <p class="text-sm text-muted-foreground">Dom's own tasks haven't saved any files yet.</p>
      {/if}
    </Card.Content>
  </Card.Root>

  <Card.Root aria-labelledby="library-docs">
    <Card.Header>
      <Card.Title id="library-docs" class="text-lg">Briefs and exports</Card.Title>
    </Card.Header>
    <Card.Content class="grid gap-3 text-sm">
      <p>
        {#if briefs}{briefs.briefs.length} {briefs.briefs.length === 1 ? "brief" : "briefs"} for a coding agent.{:else}Briefs for a coding agent.{/if}
        <a href="/app/work/briefs" class="font-medium text-signal-strong underline underline-offset-2">Open briefs</a>
      </p>
      <p class="flex flex-wrap items-center gap-2">
        Everything Dom remembers, as a zip.
        <Button variant="outline" size="sm" href="/api/memory/export" download>
          <Download data-icon="inline-start" aria-hidden="true" /> Export memory
        </Button>
      </p>
    </Card.Content>
  </Card.Root>
</div>
