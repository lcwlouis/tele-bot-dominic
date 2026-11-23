<script lang="ts">
  import Code from "$lib/components/code/Code.svelte";
  import ArrowLeft from "@lucide/svelte/icons/arrow-left";
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";
  import { onMount } from "svelte";
  import { ApiError, getJSON, send, type PluginReview as Review } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import DiffLines from "$lib/components/memory/DiffLines.svelte";
  import Notices from "$lib/components/system/Notices.svelte";
  import TrialNote from "$lib/components/system/TrialNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import Confirm from "$lib/components/work/Confirm.svelte";
  import type { Destination, Tab } from "$lib/nav";
  import { navigate } from "$lib/router.svelte";
  import { toast } from "$lib/toast.svelte";

  // The code review of one plugin version. Everything here is data printed as text: file contents, hashes, the
  // declared surfaces and the test run. Nothing a plugin supplies is run or rendered as markup. Every write names the
  // digest the owner is looking at, so a plugin that changed on disk since is refused, never approved by mistake.
  let { destination, tab, name, digest }: { destination: Destination; tab: Tab; name: string; digest: string } = $props();

  const path = $derived(`/api/system/plugins/${encodeURIComponent(name)}/${encodeURIComponent(digest)}`);
  let review = $state<Review | null>(null);
  let failed = $state("");
  let missing = $state(false);
  let busy = $state(false);
  let retiring = $state(false);
  let deleting = $state(false);

  async function load() {
    try {
      review = await getJSON<Review>(path);
      failed = "";
      missing = false;
    } catch (error) {
      missing = error instanceof Error && error.message.startsWith("404");
      failed = error instanceof Error ? error.message : "Unknown error";
      if (missing) review = null; // the version changed: never leave its stale code and buttons on screen
    }
  }
  onMount(load);

  async function act(url: string, body: object, leave: boolean) {
    if (busy) return;
    busy = true;
    try {
      const result = await send<{ ok: boolean; message: string; review?: Review }>(url, body);
      toast.show(result.ok ? "ok" : "error", result.message);
      if (result.review) review = result.review;
      if (leave) navigate("/app/system/plugins");
    } catch (error) {
      // the toast says why; a 404 or 409 means the plugin moved on, so show what is on disk now
      if (error instanceof ApiError) await load();
    } finally {
      busy = false;
    }
  }

  const same = $derived({ name, digest });
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle="Read the code before you let Dom use it." />
<PageTabs {destination} {tab} />

<p class="mb-4"><a href="/app/system/plugins" class="inline-flex items-center gap-1.5 text-sm font-medium text-signal-strong underline-offset-4 hover:underline"><ArrowLeft class="size-4" aria-hidden="true" />All plugins</a></p>

{#if review === null && failed}
  <StatusNote
    tone="error"
    title={missing ? "This version isn't available" : "Couldn't load this plugin"}
    detail={missing ? "The plugin was changed or removed since this link was made. Open the plugin list to review its current version." : `Dom's dashboard answered: ${failed}.`}
  >
    <Button variant="outline" href="/app/system/plugins">All plugins</Button>
  </StatusNote>
{:else if review === null}
  <div class="h-56 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  {@const item = review}
  <Card.Root>
    <Card.Header>
      <Card.Title class="flex flex-wrap items-center gap-2 text-xl"><span class="font-mono">{item.name}</span> <Badge variant="secondary">{item.status}</Badge></Card.Title>
      <Card.Description class="break-words">{item.description}</Card.Description>
    </Card.Header>
    <Card.Content class="grid gap-4">
      <p class="text-sm text-muted-foreground">SHA-256 <span class="font-mono break-all">{item.hash}</span></p>
      <Notices network={item.network} storage={item.storage} notices={item.notices} storageOff />
      {#if item.surfaces_changed}
        <p class="flex items-start gap-2 text-sm text-amber"><TriangleAlert class="mt-0.5 size-4 shrink-0" aria-hidden="true" />Declared commands, panels, tools, network access or storage changed since the approved version.</p>
      {/if}
      <TrialNote trial={item.test_run} pending={item.status !== "disabled"} />

      <section aria-labelledby="r-tools" class="grid gap-1 text-sm">
        <h2 id="r-tools" class="kicker">Tools</h2>
        {#each item.tools as tool (tool.name)}<p class="break-words"><span class="font-mono">{tool.name}</span> · {tool.scopes.join(", ") || "No scopes"}</p>{:else}<p class="text-muted-foreground">None</p>{/each}
      </section>
      <section aria-labelledby="r-commands" class="grid gap-1 text-sm">
        <h2 id="r-commands" class="kicker">Telegram commands</h2>
        {#each item.commands as command (command.name)}<p class="break-words"><span class="font-mono">/{command.name}</span> · {command.description} · {command.scopes.join(", ")}</p>{:else}<p class="text-muted-foreground">None</p>{/each}
      </section>
      <section aria-labelledby="r-panels" class="grid gap-1 text-sm">
        <h2 id="r-panels" class="kicker">Dashboard panels</h2>
        {#each item.panels as panel (panel.id)}<p class="break-words"><span class="font-mono">{panel.id}</span> · {panel.title}</p>{:else}<p class="text-muted-foreground">None</p>{/each}
      </section>

      {#each item.documents as file (file.path)}
        <section aria-label={`File ${file.path}`} class="grid gap-2">
          <h2 class="font-mono text-sm font-semibold">{file.path}{file.removed ? " · Removed" : ""}</h2>
          {#if !file.removed}
            <Code text={file.text} path={file.path} focusable class="max-h-96 overflow-auto rounded-lg border bg-raised/50 p-3 text-[0.8rem] break-words whitespace-pre-wrap" />
          {/if}
          {#if file.changes.length}
            <p class="text-sm text-muted-foreground">Changes since approval</p>
            <DiffLines lines={file.changes} path={file.path} all />
          {/if}
        </section>
      {/each}

      <div class="flex flex-wrap gap-2 border-t pt-4">
        {#if item.status === "pending"}
          {#if item.reviewed}
            <Button disabled={busy} onclick={() => act("/api/system/plugins/decide", { ...same, decision: "approve" }, true)}>Approve</Button>
          {:else}
            <Button disabled={busy} onclick={() => act("/api/system/plugins/reviewed", same, false)}>I've read this code</Button>
          {/if}
          <Button variant="destructive" disabled={busy} onclick={() => act("/api/system/plugins/decide", { ...same, decision: "reject" }, true)}>Reject</Button>
        {:else if item.status !== "disabled"}
          <Button variant="outline" disabled={busy} onclick={() => (retiring = true)}>Retire</Button>
        {/if}
        <Button variant="destructive" disabled={busy} onclick={() => (deleting = true)}>Delete</Button>
      </div>
    </Card.Content>
  </Card.Root>
{/if}

<Confirm
  bind:open={retiring}
  title={`Retire plugin ${name}?`}
  description="Its files stay on disk, and its tools, commands and panels turn off."
  keep="Keep it"
  action="Retire plugin"
  onconfirm={() => act("/api/system/plugins/decide", { ...same, decision: "disable" }, true)}
/>
<Confirm
  bind:open={deleting}
  title={`Delete plugin ${name}?`}
  description="Its files and database state are moved to the deletion archive before removal."
  keep="Keep it"
  action="Delete plugin"
  onconfirm={() => review && act("/api/system/plugins/delete", { name, expected: review.hash }, true)}
/>
