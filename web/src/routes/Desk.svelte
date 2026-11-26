<script lang="ts">
  import ResultText from "$lib/components/code/ResultText.svelte";
  import CircleStop from "@lucide/svelte/icons/circle-stop";
  import ExternalLink from "@lucide/svelte/icons/external-link";
  import Globe from "@lucide/svelte/icons/globe";
  import Monitor from "@lucide/svelte/icons/monitor";
  import ShieldAlert from "@lucide/svelte/icons/shield-alert";
  import Trash from "@lucide/svelte/icons/trash-2";
  import { onMount } from "svelte";
  import type { DeskData, OwnData, StepBudget } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import GoneNote from "$lib/components/kit/GoneNote.svelte";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import StripList from "$lib/components/kit/StripList.svelte";
  import type { Status } from "$lib/components/kit/status";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import Confirm from "$lib/components/work/Confirm.svelte";
  import type { Destination, Tab } from "$lib/nav";
  import { linkedId } from "$lib/deep-link";
  import { WorkPage } from "$lib/work.svelte";
  import { deskKey, ownKey, workItems, type WorkItem } from "$lib/work-items";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  const REFRESH_MS = 4000;
  const desk = new WorkPage<DeskData>("/api/work/desk");
  const own = new WorkPage<OwnData>("/api/work/own");
  const params = new URLSearchParams(location.search);
  // The container being looked at; "" is the default one. It rides along on every read and write. A handoff link names it.
  let container = $state(params.get("container") ?? "");
  // A link may name one task (a Telegram notice, a search hit): ?task=<id> a desktop task, ?own=<id> one Dom set
  // himself. The page asks for it by name, so an older one than the first page still comes, and opens it.
  // An ID no row can have (0, too large, not a number) is gone at once, without a query the API would refuse.
  const taskId = linkedId("task", params);
  const ownId = taskId === null ? linkedId("own", params) : null;
  const linked = taskId ? deskKey(taskId) : ownId ? ownKey(ownId) : null;
  let selected = $state<string | null>(null);
  let gone = $state(taskId === 0 || ownId === 0);
  let ending = $state<WorkItem | null>(null); // a cancel waiting on its confirm
  let deleting = $state<WorkItem | null>(null);

  function query() {
    const out = new URLSearchParams();
    if (container) out.set("container", container);
    if (linked?.startsWith("task-") && selected === linked) out.set("task", linked.slice(5));
    const text = out.toString();
    return text ? `?${text}` : "";
  }
  const ownQuery = () => (linked?.startsWith("own-") ? `?own=${linked.slice(4)}` : "");
  // Both lists refresh: an Own work task starts, finishes or appears while the page is open, too.
  const refresh = () => {
    if (!desk.busy) desk.load(query());
    if (!own.busy) own.load(ownQuery());
  };

  onMount(() => {
    if (linked) selected = linked; // so the first read asks for it
    Promise.all([desk.load(query()), own.load(ownQuery())]).then(() => {
      if (!linked) return;
      if (items.some((item) => item.key === linked)) selected = linked;
      else if (desk.data && own.data) {
        selected = null;
        gone = true;
      }
    });
    const timer = setInterval(() => document.visibilityState === "visible" && refresh(), REFRESH_MS);
    const onVisible = () => document.visibilityState === "visible" && refresh();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  });

  // Closing a linked item drops it from the address, so a reload doesn't open it again.
  $effect(() => {
    if (selected !== null || !linked) return;
    const url = new URL(location.href);
    if (!url.searchParams.has("task") && !url.searchParams.has("own")) return;
    url.searchParams.delete("task");
    url.searchParams.delete("own");
    history.replaceState(history.state, "", url);
  });

  const items = $derived(workItems(desk.data?.task ?? null, desk.data?.history ?? [], own.data?.tasks ?? []));
  const statuses: Status[] = ["running", "waiting", "queued", "done", "failed", "cancelled"];

  function choose(name: string) {
    container = name;
    desk.load(query());
  }

  const computer = (action: "start" | "stop") => desk.write("/api/work/desk/computer", { action, container: desk.data?.container ?? "" });
  // "12 of 30 steps", and how many steps the owner added with Keep going. Tasks from before v0.8.3 show only the count.
  const stepsLabel = (task: StepBudget & { steps: number }) =>
    task.steps_budget == null
      ? `${task.steps} steps`
      : `${task.steps} of ${task.steps_budget + task.extra_steps} steps` + (task.extra_steps ? `, ${task.extra_steps} added by you` : "");
  const resume = (task: { id: number; steps: number }) => desk.write("/api/work/desk/done", { task_id: task.id, steps: task.steps });

  const words = (text: string) => text.replaceAll("_", " ");
  const tones = { waiting: "secondary", running: "secondary", starting: "outline", refused: "destructive", error: "destructive", off: "outline" } as const;
  const kindOf = (item: WorkItem) => (item.kind === "desk" ? "Desktop task" : "Own work");

  function cancel(item: WorkItem) {
    if (item.kind === "desk") desk.write("/api/work/desk/cancel", { id: item.task.id });
    else own.write("/api/work/own/cancel", { id: item.task.id });
  }
  async function remove(item: WorkItem) {
    if (selected === item.key) selected = null;
    if (item.kind === "desk") await desk.write("/api/work/desk/delete", { id: item.task.id, expected: item.task.created_at });
    else await own.write("/api/work/own/delete", { id: item.task.id, expected: item.task.announced_at });
  }
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#if gone}<GoneNote text="That task is gone. It may have been deleted." ondismiss={() => (gone = false)} />{/if}

{#if desk.data === null && desk.failed}
  <StatusNote tone="error" title="Couldn't load the desk" detail={`Dom's dashboard answered: ${desk.failed}.`}>
    <Button variant="outline" onclick={() => desk.load(query())}>Try again</Button>
  </StatusNote>
{:else if desk.data === null}
  <div class="h-56 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  {@const data = desk.data}
  {#if desk.failed}
    <p role="alert" class="mb-4 text-sm text-coral">Couldn't refresh: {desk.failed}. Showing what Dom last said.</p>
  {/if}
  {#if own.failed}
    <p role="alert" class="mb-4 text-sm text-coral">
      {own.data ? `Couldn't refresh Dom's own work: ${own.failed}. Showing what Dom last said.` : `Couldn't load Dom's own work: ${own.failed}.`}
    </p>
  {/if}
  {#if !data.enabled}
    <p class="mb-6 flex items-center gap-2 text-meta text-muted-foreground">
      <Monitor class="size-4" aria-hidden="true" /> The desktop is off.
      <InfoGlyph label="About the desktop">Dom has no computer to work on. Turn on desktop mode in his config to give him one.</InfoGlyph>
    </p>
  {:else}
    {#if data.containers.length > 1}
      <section aria-labelledby="desk-containers" class="mb-6">
        <h2 id="desk-containers" class="mb-3 text-title font-semibold">Computers</h2>
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
                <span class="font-mono text-xs text-muted-foreground">{item.host}{item.task ? `, #${item.task.id} ${words(item.task.status)}` : ""}</span>
                {#if item.detail}<span class="text-sm break-words text-muted-foreground">{item.detail}</span>{/if}
              </button>
            </li>
          {/each}
        </ul>
        <ul aria-label="Host limits" class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
          {#each data.hosts as host (host.name)}
            <li>
              {host.name}: <span class="font-mono">{host.running}/{host.max_containers}</span>
              {host.max_containers === 1 ? "container" : "containers"}{#if host.memory_floor}, <span class="font-mono">{host.memory_floor}</span> free floor{/if}
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    <!-- The computer: its state, the handoff when Dom needs the owner, and start or stop. -->
    <Card.Root class="orbit-grid mb-8" aria-labelledby="desk-computer">
      <Card.Content class="grid gap-3">
        <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
          <Monitor class="size-5 text-signal" aria-hidden="true" />
          <h2 id="desk-computer" class="text-title font-semibold">{data.containers.length > 1 ? data.container : "Computer"} {data.status}</h2>
          <div class="ml-auto flex flex-wrap gap-2">
            {#if data.status === "stopped" || data.status === "error"}
              <Button size="sm" disabled={desk.busy} onclick={() => computer("start")}>{data.on_this_machine ? "Start computer" : "Connect to computer"}</Button>
            {/if}
            {#if data.status === "ready"}
              <Button size="sm" href={data.viewer} variant="secondary">Full-screen viewer <ExternalLink data-icon="inline-end" aria-hidden="true" /></Button>
            {/if}
            {#if data.on_this_machine && (data.status === "ready" || data.status === "error") && !data.task}
              <Button size="sm" variant="outline" disabled={desk.busy} onclick={() => computer("stop")}>Stop computer</Button>
            {/if}
          </div>
        </div>
        {#if data.error}
          <p role="alert" class="rounded-lg border border-coral/45 bg-coral-tint p-3 text-sm break-words text-coral">{data.error}</p>
        {/if}
        {#if data.task?.status === "waiting_owner"}
          {@const task = data.task}
          <div class="rounded-xl border border-status-waiting/50 bg-tint p-4">
            <p class="flex items-center gap-1.5 font-semibold text-signal-strong">
              Dom needs you
              <InfoGlyph label="About handoffs">Logins you make here are kept for later tasks.</InfoGlyph>
            </p>
            <p class="mt-1 text-sm break-words">Dom needs you to log in{data.handoff_host ? ` on ${data.handoff_host}` : ""}: {task.error}</p>
            <p class="mt-1 text-meta break-words text-muted-foreground">Task #{task.id}: {task.brief}</p>
            <div class="mt-3 flex flex-wrap gap-2">
              <Button href={data.viewer}>Open live viewer <ExternalLink data-icon="inline-end" aria-hidden="true" /></Button>
              <Button variant="secondary" disabled={desk.busy} onclick={() => resume(task)}>I'm done, resume Dom</Button>
            </div>
          </div>
        {/if}
      </Card.Content>
    </Card.Root>
  {/if}

  <section aria-labelledby="work-tasks" class="grid gap-3">
    <div class="flex flex-wrap items-center gap-1.5">
      <h2 id="work-tasks" class="text-title font-semibold">Tasks</h2>
      <InfoGlyph label="About tasks">Desktop tasks you gave Dom and work he set himself, newest first. Open one for its steps, result and screenshots.</InfoGlyph>
    </div>
    {#if own.data?.guard && !own.data.guard.ok}
      <!-- A warning the owner must act on stays as words. -->
      <p role="alert" class="flex items-center gap-2 text-sm text-coral">
        <ShieldAlert class="size-4 shrink-0" aria-hidden="true" /> Dom's own work is held: the network guard isn't verified ({own.data.guard.detail}).
      </p>
    {/if}
    <StripList
      {items}
      key={(item) => item.key}
      strip={(item) => ({ status: item.status, title: item.title, at: item.at, meta: kindOf(item) })}
      label="Tasks, newest first"
      bind:selected
      search="Filter tasks"
      {statuses}
      level={3}
      name="Tasks"
      preview={false}
    >
      {#snippet actions(item)}
        {#if item.live}
          <span class="size-8" aria-hidden="true"></span>
        {:else}
          <IconButton
            label={`Delete ${item.kind === "desk" ? "desktop task" : "own work task"} ${item.task.id}`}
            icon={Trash}
            class="bg-transparent text-muted-foreground hover:bg-coral-tint hover:text-coral"
            disabled={desk.busy || own.busy}
            onclick={() => (deleting = item)}
          />
        {/if}
      {/snippet}
      {#snippet details(item)}
        <dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-body">
          <dt class="text-muted-foreground">Task</dt>
          <dd class="tabular-nums">#{item.task.id}, {item.word}</dd>
          {#if item.kind === "desk"}
            {@const task = item.task}
            <dt class="text-muted-foreground">Steps</dt>
            <dd class="tabular-nums">{stepsLabel(task)}</dd>
            {#if "container" in task && task.container && task.container !== "default"}<dt class="text-muted-foreground">Computer</dt><dd>{task.container}</dd>{/if}
            {#if "finished" in task && task.finished}<dt class="text-muted-foreground">Ended</dt><dd>{task.finished}</dd>{/if}
          {:else}
            {@const task = item.task}
            <dt class="text-muted-foreground">Why</dt>
            <dd class="break-words">{task.why}</dd>
            <dt class="text-muted-foreground">Uses</dt>
            <dd>{task.tools.join(", ")}</dd>
            <dt class="text-muted-foreground">Sites</dt>
            <dd class="break-words">{task.sites.length ? task.sites.join(", ") : "None, offline"}</dd>
            <dt class="text-muted-foreground">Steps</dt>
            <dd class="tabular-nums">{task.steps ? `${task.steps} of ${task.steps_budget}` : `Up to ${task.steps_budget}`}, within {task.minutes} min</dd>
            <dt class="text-muted-foreground">Announced</dt>
            <dd>{task.announced}</dd>
            {#if task.status === "planned"}<dt class="text-muted-foreground">Starts</dt><dd>{task.starts ? `After ${task.starts}` : "Not announced yet, so it won't start"}</dd>{/if}
            {#if task.finished}<dt class="text-muted-foreground">Ended</dt><dd>{task.finished}</dd>{/if}
          {/if}
        </dl>

        {#if item.kind === "desk" && item.task.status === "waiting_owner"}
          <p class="mt-4 rounded-lg border border-status-waiting/50 bg-raised px-3 py-2 text-body">Dom is waiting for you to log in. The handoff is above the list.</p>
        {/if}
        {#if "result" in item.task && item.task.result}
          <!-- What a task brings back is text from the web: it stays marked as unchecked. -->
          <figure class="mt-4 grid gap-1.5">
            <figcaption class="flex items-center gap-1.5 text-meta text-muted-foreground">
              <Globe class="size-3.5" aria-hidden="true" /> Result, as the web gave it (not checked)
            </figcaption>
            <ResultText text={item.task.result} class="max-h-80 overflow-auto rounded-lg border border-dashed bg-canvas/50 p-3 text-sm" />
          </figure>
        {/if}
        {#if item.task.error && item.task.status !== "waiting_owner"}
          <p class="mt-4 max-h-72 overflow-auto rounded-lg border border-coral/45 bg-coral-tint p-3 text-sm wrap-anywhere whitespace-pre-wrap text-coral">{item.task.error}</p>
        {/if}
        {#if item.kind === "own" && item.task.files.length}
          {@const task = item.task}
          <h3 class="mt-4 text-meta font-medium text-muted-foreground">Screenshots</h3>
          <ul class="mt-1.5 flex flex-wrap gap-2" aria-label={`Screenshots from task ${task.id}`}>
            {#each task.files as name (name)}
              <li>
                <a href={`/api/work/own/${task.id}/files/${name}`} target="_blank" rel="noopener" class="block rounded-lg outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
                  <img src={`/api/work/own/${task.id}/files/${name}`} alt={`Screenshot ${name} from task ${task.id}`} loading="lazy" class="h-24 w-auto rounded-lg border" />
                </a>
              </li>
            {/each}
          </ul>
        {/if}

        <div class="mt-5 flex flex-wrap gap-2">
          {#if item.live}
            <Button size="sm" variant="outline" disabled={desk.busy || own.busy} onclick={() => (ending = item)}>
              <CircleStop data-icon="inline-start" aria-hidden="true" /> Cancel task
            </Button>
          {:else}
            <Button size="sm" variant="destructive" disabled={desk.busy || own.busy} onclick={() => (deleting = item)}>
              <Trash data-icon="inline-start" aria-hidden="true" /> Delete task
            </Button>
          {/if}
        </div>
      {/snippet}
      {#snippet empty()}<StatusNote title="No tasks yet" detail="Ask Dom to do something on the desktop, or let him plan his own work." />{/snippet}
    </StripList>
  </section>
{/if}

<Confirm
  bind:open={() => ending !== null, (open) => !open && (ending = null)}
  title={`Cancel task #${ending?.task.id}?`}
  description="Dom stops it if it is running and won't start it later."
  keep="Keep it"
  action="Cancel task"
  onconfirm={() => ending && cancel(ending)}
/>
<Confirm
  bind:open={() => deleting !== null, (open) => !open && (deleting = null)}
  title={`Delete task #${deleting?.task.id}?`}
  description={deleting?.kind === "own" ? "It and its screenshots are moved to the deletion archive." : "Its files are moved to the deletion archive."}
  keep="Keep it"
  action="Delete task"
  onconfirm={() => deleting && remove(deleting)}
/>
