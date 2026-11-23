<script lang="ts">
  import ExternalLink from "@lucide/svelte/icons/external-link";
  import Info from "@lucide/svelte/icons/info";
  import Monitor from "@lucide/svelte/icons/monitor";
  import { onMount } from "svelte";
  import type { DeskData, DeskHistory } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import Confirm from "$lib/components/work/Confirm.svelte";
  import type { Destination, Tab } from "$lib/nav";
  import { WorkPage } from "$lib/work.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  const REFRESH_MS = 4000;
  const page = new WorkPage<DeskData>("/api/work/desk");
  // The container being looked at; "" is the default one. It rides along on every read and write.
  let container = $state("");
  let deleting = $state<DeskHistory | null>(null);

  const query = () => (container ? `?container=${encodeURIComponent(container)}` : "");
  const refresh = () => !page.busy && page.load(query());

  onMount(() => {
    page.load();
    const timer = setInterval(() => document.visibilityState === "visible" && refresh(), REFRESH_MS);
    const onVisible = () => document.visibilityState === "visible" && refresh();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  });

  function choose(name: string) {
    container = name;
    page.load(query());
  }

  const computer = (action: "start" | "stop") => page.write("/api/work/desk/computer", { action, container: page.data?.container ?? "" });
  const resume = (task: { id: number; steps: number }) => page.write("/api/work/desk/done", { task_id: task.id, steps: task.steps });

  const words = (text: string) => text.replaceAll("_", " ");
  const tones = { waiting: "secondary", running: "secondary", starting: "outline", refused: "destructive", error: "destructive", off: "outline" } as const;
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#if page.data === null && page.failed}
  <StatusNote tone="error" title="Couldn't load the desk" detail={`Dom's dashboard answered: ${page.failed}.`}>
    <Button variant="outline" onclick={() => page.load(query())}>Try again</Button>
  </StatusNote>
{:else if page.data === null}
  <div class="h-56 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  {@const data = page.data}
  {#if !data.enabled}
    <StatusNote title="The desktop is off" detail="Dom has no computer to work on. Turn on desktop mode in his config to give him one." />
  {:else}
    {#if page.failed}
      <p role="alert" class="mb-4 text-sm text-coral">Couldn't refresh: {page.failed}. Showing what Dom last said.</p>
    {/if}

    {#if data.containers.length > 1}
      <section aria-labelledby="desk-containers" class="mb-6">
        <h2 id="desk-containers" class="mb-3 text-lg font-semibold">Computers</h2>
        <ul class="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {#each data.containers as item (item.name)}
            <li>
              <button
                type="button"
                aria-pressed={item.selected}
                onclick={() => choose(item.name)}
                class="flex w-full flex-col gap-1 rounded-xl border bg-surface p-3 text-left outline-none transition-colors hover:border-signal/60 focus-visible:ring-3 focus-visible:ring-ring/50 aria-pressed:border-signal aria-pressed:bg-tint"
              >
                <span class="flex items-center gap-2 font-medium">
                  {item.name}
                  <Badge variant={tones[item.tone]}>{item.state}</Badge>
                </span>
                <span class="font-mono text-xs text-muted-foreground">{item.host}{item.task ? ` · #${item.task.id} ${words(item.task.status)}` : ""}</span>
                {#if item.detail}<span class="text-sm break-words text-muted-foreground">{item.detail}</span>{/if}
              </button>
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    <Card.Root class="orbit-grid">
      <Card.Header>
        <p class="kicker">{data.containers.length > 1 ? data.container : "Computer"}</p>
        <Card.Title class="flex flex-wrap items-center gap-2 text-xl">
          <Monitor class="size-5 text-signal" aria-hidden="true" />
          Computer {data.status}
        </Card.Title>
      </Card.Header>
      <Card.Content class="grid gap-4">
        {#if data.error}
          <p role="alert" class="rounded-lg border border-coral/45 bg-coral-tint p-3 text-sm break-words text-coral">{data.error}</p>
        {/if}

        {#if data.task}
          <div class="grid gap-1">
            <p class="font-mono text-sm text-muted-foreground">Task #{data.task.id} · {words(data.task.status)} · {data.task.steps} steps</p>
            <p class="text-lg font-medium break-words">{data.task.brief}</p>
          </div>
          {#if data.task.status === "waiting_owner"}
            <div class="rounded-xl border border-signal/45 bg-tint p-4">
              <p class="flex items-center gap-2 font-semibold text-signal-strong">
                <Info class="size-4" aria-hidden="true" /> Dom needs you
              </p>
              <p class="mt-1 text-sm break-words">
                Dom needs you to log in{data.handoff_host ? ` on ${data.handoff_host}` : ""}: {data.task.error}
              </p>
              <p class="mt-1 text-sm text-muted-foreground">Logins you make here are kept for later tasks.</p>
              <div class="mt-3 flex flex-wrap gap-2">
                <Button href={data.viewer}>Open live viewer <ExternalLink data-icon="inline-end" aria-hidden="true" /></Button>
                <Button variant="secondary" disabled={page.busy} onclick={() => data.task && resume(data.task)}>I'm done, resume Dom</Button>
              </div>
            </div>
          {/if}
        {:else}
          <p class="text-muted-foreground">No task running.</p>
        {/if}
      </Card.Content>
      <Card.Footer class="flex flex-wrap gap-2">
        {#if data.status === "stopped" || data.status === "error"}
          <Button disabled={page.busy} onclick={() => computer("start")}>{data.local ? "Start computer" : "Connect to computer"}</Button>
        {/if}
        {#if data.status === "ready"}
          <Button href={data.viewer} variant="secondary">Full-screen viewer <ExternalLink data-icon="inline-end" aria-hidden="true" /></Button>
        {/if}
        {#if data.local && (data.status === "ready" || data.status === "error") && !data.task}
          <Button variant="outline" disabled={page.busy} onclick={() => computer("stop")}>Stop computer</Button>
        {/if}
      </Card.Footer>
    </Card.Root>
  {/if}

  <section aria-labelledby="desk-history" class="mt-10 grid gap-4">
    <h2 id="desk-history" class="text-lg font-semibold">Task history</h2>
    {#if data.history.length === 0}
      <StatusNote title="No finished desktop tasks yet" detail="Tasks Dom finishes on the desktop are listed here." />
    {:else}
      <ul class="grid gap-3" aria-label="Task history">
        {#each data.history as row (row.id)}
          <li>
            <Card.Root>
              <Card.Content class="grid gap-2">
                <p class="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                  <span class="font-mono">#{row.id}</span>
                  <Badge variant={row.status === "done" ? "secondary" : row.status === "failed" ? "destructive" : "outline"}>{words(row.status)}</Badge>
                  <span>{row.container && row.container !== "default" ? `${row.container} · ` : ""}{row.steps} steps{row.finished ? ` · ${row.finished}` : ""}</span>
                </p>
                <p class="font-medium break-words">{row.brief}</p>
                {#if row.result}<p class="rounded-lg border bg-canvas/50 p-3 text-sm break-words whitespace-pre-line">{row.result}</p>{/if}
                {#if row.error}<p class="rounded-lg border border-coral/45 bg-coral-tint p-3 text-sm break-words whitespace-pre-line text-coral">{row.error}</p>{/if}
              </Card.Content>
              <Card.Footer>
                <Button variant="destructive" size="sm" disabled={page.busy} aria-label={`Delete desktop task ${row.id}`} onclick={() => (deleting = row)}>Delete</Button>
              </Card.Footer>
            </Card.Root>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
{/if}

<Confirm
  bind:open={() => deleting !== null, (open) => !open && (deleting = null)}
  title={`Delete desktop task #${deleting?.id}?`}
  description="Its files are moved to the deletion archive."
  keep="Keep it"
  action="Delete task"
  onconfirm={() => deleting && page.write("/api/work/desk/delete", { id: deleting.id, expected: deleting.created_at })}
/>
