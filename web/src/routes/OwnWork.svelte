<script lang="ts">
  import Code from "$lib/components/code/Code.svelte";
  import ShieldAlert from "@lucide/svelte/icons/shield-alert";
  import ShieldCheck from "@lucide/svelte/icons/shield-check";
  import { onMount } from "svelte";
  import type { OwnData, OwnTask, Skill } from "$lib/api";
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

  const page = new WorkPage<OwnData>("/api/work/own");
  let cancelling = $state<OwnTask | null>(null);
  let deleting = $state<OwnTask | null>(null);
  let retiring = $state<Skill | null>(null);
  let removing = $state<Skill | null>(null);

  onMount(() => page.load());

  const live = (task: OwnTask) => task.status === "planned" || task.status === "running";
  const tone = (status: string) => (status === "done" ? "secondary" : status === "failed" ? "destructive" : "outline");
  const skillAction = (skill: Skill, action: "retire" | "restore" | "delete") =>
    page.write("/api/work/skills/action", { name: skill.name, action, expected: skill.hash });
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#if page.data === null && page.failed}
  <StatusNote tone="error" title="Couldn't load Dom's own work" detail={`Dom's dashboard answered: ${page.failed}.`}>
    <Button variant="outline" onclick={() => page.load()}>Try again</Button>
  </StatusNote>
{:else if page.data === null}
  <div class="h-40 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  {@const data = page.data}
  <section aria-labelledby="own-tasks" class="grid gap-4">
    <h2 id="own-tasks" class="text-lg font-semibold">Tasks</h2>
    <p class="max-w-2xl text-sm text-muted-foreground">
      {#if !data.enabled}
        Self-directed work is off (<code class="font-mono">[self_work] enabled = false</code>).
      {:else if !data.active}
        Self-directed work needs a local desktop host. Nothing is planned until one is configured.
      {:else}
        Dom plans up to three small tasks in his nightly daydream. Each starts no sooner than {Math.round(data.delay_hours * 10) / 10}
        hours after it is announced, only while he is awake and free, and only in the sandbox once the network guard is verified.
      {/if}
    </p>
    {#if data.guard}
      <p class="flex items-center gap-2 text-sm {data.guard.ok ? 'text-muted-foreground' : 'text-coral'}">
        {#if data.guard.ok}<ShieldCheck class="size-4 text-signal" aria-hidden="true" />{:else}<ShieldAlert class="size-4" aria-hidden="true" />{/if}
        Network guard: {data.guard.ok ? "verified from inside the sandbox" : `not verified (${data.guard.detail})`}
        <span class="font-mono">{data.guard.at}</span>
      </p>
    {:else if data.active}
      <p class="text-sm text-muted-foreground">Network guard: not checked yet. Dom checks it from inside the sandbox before each task.</p>
    {/if}

    {#if data.tasks.length === 0}
      <StatusNote title="No tasks planned yet" detail="Dom plans work for himself in his nightly daydream." />
    {:else}
      <ul class="grid gap-3" aria-label="Tasks">
        {#each data.tasks as task (task.id)}
          <li>
            <Card.Root>
              <Card.Header>
                <Card.Title class="flex flex-wrap items-center gap-2 text-base">
                  <span class="font-mono text-muted-foreground">#{task.id}</span>
                  <span class="min-w-0 break-words">{task.goal}</span>
                  <Badge variant={tone(task.status)}>{task.status}</Badge>
                </Card.Title>
              </Card.Header>
              <Card.Content class="grid gap-2 text-sm">
                <p><span class="text-muted-foreground">Why:</span> {task.why}</p>
                <p><span class="text-muted-foreground">Uses:</span> {task.tools.join(", ")}</p>
                <p><span class="text-muted-foreground">Sites:</span> {task.sites.length ? task.sites.join(", ") : "none, offline"}</p>
                <p>
                  <span class="text-muted-foreground">Budget:</span> up to {task.minutes} min and {task.steps_budget} steps{task.steps ? `; used ${task.steps} steps` : ""}
                </p>
                <p class="text-muted-foreground">
                  Announced {task.announced}{#if task.status === "planned"}; {task.starts ? `starts after ${task.starts}` : "not announced yet, so it will not start"}{/if}{#if task.finished}; ended {task.finished}{/if}
                </p>
                {#if task.result}<p class="rounded-lg border bg-canvas/50 p-3 break-words whitespace-pre-line">{task.result}</p>{/if}
                {#if task.error}<p class="rounded-lg border border-coral/45 bg-coral-tint p-3 break-words whitespace-pre-line text-coral">{task.error}</p>{/if}
                {#if task.files.length}
                  <ul class="flex flex-wrap gap-2" aria-label={`Screenshots from task ${task.id}`}>
                    {#each task.files as name (name)}
                      <li>
                        <a href={`/api/work/own/${task.id}/files/${name}`} target="_blank" rel="noopener" class="block rounded-lg outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
                          <img
                            src={`/api/work/own/${task.id}/files/${name}`}
                            alt={`Screenshot ${name} from task ${task.id}`}
                            loading="lazy"
                            class="h-24 w-auto rounded-lg border"
                          />
                        </a>
                      </li>
                    {/each}
                  </ul>
                {/if}
              </Card.Content>
              <Card.Footer>
                {#if live(task)}
                  <Button variant="destructive" size="sm" disabled={page.busy} onclick={() => (cancelling = task)}>Cancel #{task.id}</Button>
                {:else}
                  <Button variant="destructive" size="sm" disabled={page.busy} onclick={() => (deleting = task)}>Delete #{task.id}</Button>
                {/if}
              </Card.Footer>
            </Card.Root>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section aria-labelledby="own-skills" class="mt-10 grid gap-4">
    <h2 id="own-skills" class="text-lg font-semibold">Skills</h2>
    {#if data.skills.length === 0}
      <StatusNote title="No skills yet" detail="Skills are procedures Dom learns and you approve." />
    {:else}
      <ul class="grid gap-3" aria-label="Skills">
        {#each data.skills as skill (skill.name)}
          <li>
            <Card.Root>
              <Card.Header>
                <Card.Title class="flex flex-wrap items-center gap-2 text-base">
                  <span class="font-mono">{skill.name}</span>
                  {#if skill.retired}<Badge variant="outline">retired</Badge>{/if}
                </Card.Title>
                <Card.Description>{skill.description}</Card.Description>
              </Card.Header>
              <Card.Content>
                <details class="group text-sm">
                  <summary class="cursor-pointer rounded-md text-signal-strong outline-none focus-visible:ring-3 focus-visible:ring-ring/50">View procedure<span class="sr-only"> for {skill.name}</span></summary>
                  <Code text={skill.body} language="markdown" focusable class="mt-2 overflow-x-auto rounded-lg border bg-canvas/50 p-3 text-[0.8rem] break-words whitespace-pre-wrap" />
                </details>
              </Card.Content>
              <Card.Footer class="flex flex-wrap gap-2">
                {#if skill.retired}
                  <Button variant="secondary" size="sm" disabled={page.busy} onclick={() => skillAction(skill, "restore")}>Restore<span class="sr-only"> {skill.name}</span></Button>
                {:else}
                  <Button variant="outline" size="sm" disabled={page.busy} onclick={() => (retiring = skill)}>Retire<span class="sr-only"> {skill.name}</span></Button>
                {/if}
                <Button variant="destructive" size="sm" disabled={page.busy} onclick={() => (removing = skill)}>Delete<span class="sr-only"> {skill.name}</span></Button>
              </Card.Footer>
            </Card.Root>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
{/if}

<Confirm
  bind:open={() => cancelling !== null, (open) => !open && (cancelling = null)}
  title={`Cancel task #${cancelling?.id}?`}
  description="Dom stops it if it is running and won't start it later."
  keep="Keep it"
  action="Cancel task"
  onconfirm={() => cancelling && page.write("/api/work/own/cancel", { id: cancelling.id })}
/>
<Confirm
  bind:open={() => deleting !== null, (open) => !open && (deleting = null)}
  title={`Delete task #${deleting?.id} and its screenshots?`}
  description="They are moved to the deletion archive."
  keep="Keep it"
  action="Delete task"
  onconfirm={() => deleting && page.write("/api/work/own/delete", { id: deleting.id, expected: deleting.announced_at })}
/>
<Confirm
  bind:open={() => retiring !== null, (open) => !open && (retiring = null)}
  title={`Retire skill ${retiring?.name}?`}
  description="Its files stay on disk, and Dom stops using it until you restore it."
  keep="Keep it active"
  action="Retire skill"
  onconfirm={() => retiring && skillAction(retiring, "retire")}
/>
<Confirm
  bind:open={() => removing !== null, (open) => !open && (removing = null)}
  title={`Delete skill ${removing?.name}?`}
  description="Its files are moved to the deletion archive before removal."
  keep="Keep it"
  action="Delete skill"
  onconfirm={() => removing && skillAction(removing, "delete")}
/>
