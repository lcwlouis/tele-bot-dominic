<script lang="ts">
  import ImagePlus from "@lucide/svelte/icons/image-plus";
  import MapPin from "@lucide/svelte/icons/map-pin";
  import Pencil from "@lucide/svelte/icons/pencil";
  import Plus from "@lucide/svelte/icons/plus";
  import Trash from "@lucide/svelte/icons/trash-2";
  import { onMount } from "svelte";
  import { ApiError, getJSON, send, type WorldData, type WorldPlace } from "$lib/api";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import Markdown from "$lib/components/markdown/Markdown.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { Label } from "$lib/components/ui/label";
  import { setMemoryTags } from "$lib/memory-tags";
  import type { Destination, Tab } from "$lib/nav";
  import { toast } from "$lib/toast.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  let world = $state<WorldData | null>(null);
  let failed = $state("");
  let busy = $state(false);
  // The sheet's editor: `draft` is null while the sheet is only shown.
  let draft = $state<string | null>(null);
  let newName = $state("");
  let newNote = $state("");
  // A photo's address carries a counter, so a replaced picture is fetched again.
  let photoVersion = $state(0);

  setMemoryTags(() => ({ chats: {}, entities: world?.sheet.entities ?? {} }));

  async function load() {
    try {
      world = await getJSON<WorldData>("/api/world");
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }
  onMount(load);

  /** One write at a time; the answer replaces the page's data. A refusal toasts its reason and leaves the page as it was. */
  async function write(path: string, body: object, done: string): Promise<boolean> {
    if (busy) return false;
    busy = true;
    try {
      world = await send<WorldData>(path, body);
      toast.show("ok", done);
      return true;
    } catch {
      return false;
    } finally {
      busy = false;
    }
  }

  async function saveSheet() {
    if (!world || draft === null || busy) return;
    busy = true;
    try {
      const saved = await send<{ message: string; content: string; version: string; entities: Record<string, string> }>(
        "/api/memory/save",
        { file: world.sheet.file, version: world.sheet.version, content: draft },
      );
      world = { ...world, sheet: { ...world.sheet, content: saved.content, version: saved.version, entities: saved.entities } };
      draft = null;
      toast.show("ok", "Saved the sheet.");
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) await load(); // it changed under us: show the current text
    } finally {
      busy = false;
    }
  }

  async function addPlace(event: SubmitEvent) {
    event.preventDefault();
    if (await write("/api/world/place", { name: newName.trim(), note: newNote.trim() }, "Added the place.")) {
      newName = newNote = "";
    }
  }

  function bytes(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1] ?? "");
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  async function choosePhoto(place: WorldPlace, event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = ""; // the same file can be chosen again
    if (!file || !world) return;
    if (file.size > world.max_photo_mb * 1024 * 1024) {
      toast.show("error", `That picture is over ${world.max_photo_mb} MB.`);
      return;
    }
    let data: string;
    try {
      data = await bytes(file);
    } catch {
      toast.show("error", "Couldn't read that file. Choose it again.");
      return;
    }
    if (await write("/api/world/photo", { slug: place.slug, data }, `Saved the photo of ${place.name}.`)) photoVersion += 1;
  }

  const day = (iso: string) => new Date(iso + "T12:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#if world === null && failed}
  <StatusNote tone="error" title="Couldn't load Dom's world" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="secondary" onclick={load}>Retry</Button>
  </StatusNote>
{:else if world === null}
  <div class="h-40 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  <div class="grid gap-6">
    <Card.Root>
      <Card.Header>
        <Card.Title class="flex items-center gap-2 text-lg">
          Life sheet
          <InfoGlyph label="About the life sheet">
            Dom's own life: where he lives and works, his routine by weekday and part of the day, his regular spots and
            projects. It is the file WORLD.md. A line you write or change is pinned and Dom can't rewrite it. Dom may
            change the rest at most once every {world.interval_days} days, with a reason. In a turn he sees the sheet and the
            routine line for right now, so he doesn't repeat himself.
          </InfoGlyph>
        </Card.Title>
        <Card.Description>
          {#if world.last_change}
            Dom last changed it on {day(world.last_change.day)}: {world.last_change.reason}
          {:else}
            Dom hasn't changed it yet.
          {/if}
        </Card.Description>
      </Card.Header>
      <Card.Content>
        {#if draft !== null}
          <Label for="world-sheet" class="sr-only">Life sheet, as text</Label>
          <textarea
            id="world-sheet"
            bind:value={draft}
            rows="14"
            spellcheck="true"
            class="min-h-64 w-full rounded-lg border bg-raised p-3 font-mono text-sm leading-relaxed"
          ></textarea>
          <p class="mt-2 text-sm text-muted-foreground">
            {draft.length.toLocaleString("en-US")} of {world.sheet.limit?.toLocaleString("en-US")} characters, not counting tags.
          </p>
        {:else if world.sheet.content.trim()}
          <Markdown text={world.sheet.content} class="text-[0.95rem]" />
        {:else}
          <p class="text-sm text-muted-foreground">Nothing yet. Edit it to give Dom a place to live, or wait for him to write his own.</p>
        {/if}
      </Card.Content>
      <Card.Footer class="gap-2">
        {#if draft !== null}
          <Button onclick={saveSheet} disabled={busy}>Save sheet</Button>
          <Button variant="outline" onclick={() => (draft = null)} disabled={busy}>Discard</Button>
        {:else}
          <Button variant="outline" onclick={() => (draft = world?.sheet.content ?? "")}>
            <Pencil aria-hidden="true" /> Edit sheet
          </Button>
        {/if}
      </Card.Footer>
    </Card.Root>

    <Card.Root>
      <Card.Header>
        <Card.Title class="flex items-center gap-2 text-lg">
          Places
          <InfoGlyph label="About places">
            Real places Dom knows, from his notes. A reference photo is optional and yours to add: when Dom sends a picture
            set at the place, the image model sees it, if that model takes a reference. The photo is saved without its camera
            data, and only chats that know the place can use it.
          </InfoGlyph>
        </Card.Title>
      </Card.Header>
      <Card.Content class="grid gap-4">
        {#if world.places.length === 0}
          <p class="text-sm text-muted-foreground">No places yet. Add one below, or Dom's notes will grow some.</p>
        {:else}
          <ul class="grid gap-3" aria-label="Places">
            {#each world.places as place (place.slug)}
              <li class="flex flex-wrap items-center gap-3 rounded-lg border bg-surface p-3">
                {#if place.photo}
                  <img
                    src={`/api/world/places/${place.slug}/photo?v=${photoVersion}`}
                    alt={`Reference photo of ${place.name}`}
                    class="size-20 rounded-md border object-cover"
                    width="80"
                    height="80"
                  />
                {:else}
                  <div class="grid size-20 place-items-center rounded-md border bg-raised text-muted-foreground" aria-hidden="true">
                    <MapPin class="size-6" />
                  </div>
                {/if}
                <div class="min-w-0 flex-1">
                  <h3 class="font-medium capitalize">{place.name}</h3>
                  <p class="text-sm break-words text-muted-foreground">{place.note || "No notes yet."}</p>
                </div>
                <div class="flex items-center gap-1">
                  <label class="inline-flex">
                    <span class="sr-only">{place.photo ? "Replace" : "Add"} the photo of {place.name}</span>
                    <input type="file" accept="image/jpeg,image/png,image/webp" class="peer sr-only" disabled={busy} onchange={(event) => choosePhoto(place, event)} />
                    <span
                      class="inline-flex h-9 cursor-pointer items-center gap-2 rounded-md border bg-background px-3 text-sm font-medium peer-focus-visible:ring-2 peer-focus-visible:ring-focus"
                      aria-hidden="true"
                    >
                      <ImagePlus class="size-4" /> {place.photo ? "Replace photo" : "Add photo"}
                    </span>
                  </label>
                  {#if place.photo}
                    <IconButton
                      label={`Remove the photo of ${place.name}`}
                      icon={Trash}
                      disabled={busy}
                      onclick={() => write("/api/world/photo/remove", { slug: place.slug }, `Removed the photo of ${place.name}.`)}
                    />
                  {/if}
                </div>
              </li>
            {/each}
          </ul>
        {/if}

        <form class="grid gap-3 rounded-lg border border-dashed p-3 sm:grid-cols-[12rem_1fr_auto] sm:items-end" onsubmit={addPlace}>
          <div class="grid gap-1.5">
            <Label for="new-place-name">Place name</Label>
            <Input id="new-place-name" bind:value={newName} maxlength={60} placeholder="The blue-door gym" required />
          </div>
          <div class="grid gap-1.5">
            <Label for="new-place-note">One line about it</Label>
            <Input id="new-place-note" bind:value={newNote} maxlength={300} placeholder="Small climbing gym, open late" required />
          </div>
          <Button type="submit" disabled={busy || !newName.trim() || !newNote.trim()}><Plus aria-hidden="true" /> Add place</Button>
        </form>
        <p class="text-sm text-muted-foreground">A place you add is pinned, and every chat that hears of it may know it.</p>
      </Card.Content>
    </Card.Root>
  </div>
{/if}
