<script lang="ts">
  import Code from "$lib/components/code/Code.svelte";
  import ShieldAlert from "@lucide/svelte/icons/shield-alert";
  import ShieldCheck from "@lucide/svelte/icons/shield-check";
  import { onMount } from "svelte";
  import type { OwnData, Skill } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import Confirm from "$lib/components/work/Confirm.svelte";
  import type { Destination, Tab } from "$lib/nav";
  import { navigate } from "$lib/router.svelte";
  import { WorkPage } from "$lib/work.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  // Dom's own tasks live in Work's one task list (E76); this page is how his own work runs, and his skills.
  // A link to one task (?own=<id>, from a Telegram notice) forwards to that list, which opens it.
  const linked = new URLSearchParams(location.search).get("own");
  const page = new WorkPage<OwnData>("/api/work/own");
  let retiring = $state<Skill | null>(null);
  let removing = $state<Skill | null>(null);

  onMount(() => {
    if (linked && /^\d+$/.test(linked)) navigate(`/app/work?own=${linked}`, { replace: true });
    else page.load();
  });

  const skillAction = (skill: Skill, action: "retire" | "restore" | "delete") =>
    page.write("/api/work/skills/action", { name: skill.name, action, expected: skill.hash });
  const planned = $derived(page.data?.tasks.filter((task) => task.status === "planned" || task.status === "running").length ?? 0);
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
  <section aria-labelledby="own-tasks" class="grid gap-3">
    <div class="flex flex-wrap items-center gap-1.5">
      <h2 id="own-tasks" class="text-title font-semibold">How it runs</h2>
      <InfoGlyph label="About Dom's own work">
        Dom plans up to three small tasks in his nightly daydream. Each starts no sooner than {Math.round(data.delay_hours * 10) / 10} hours after it is
        announced, only while he is awake and free, and only in the sandbox once the network guard is verified.
      </InfoGlyph>
    </div>
    {#if !data.enabled}
      <p class="text-body text-muted-foreground">Self-directed work is off (<code class="font-mono">[self_work] enabled = false</code>).</p>
    {:else if !data.active}
      <p class="text-body text-muted-foreground">Self-directed work needs a local desktop host. Nothing is planned until one is configured.</p>
    {/if}
    {#if data.guard}
      <p class="flex flex-wrap items-center gap-2 text-sm {data.guard.ok ? 'text-muted-foreground' : 'text-coral'}" role={data.guard.ok ? undefined : "alert"}>
        {#if data.guard.ok}<ShieldCheck class="size-4 text-signal" aria-hidden="true" />{:else}<ShieldAlert class="size-4" aria-hidden="true" />{/if}
        Network guard: {data.guard.ok ? "verified from inside the sandbox" : `not verified (${data.guard.detail})`}
        <span class="font-mono">{data.guard.at}</span>
      </p>
    {:else if data.active}
      <p class="flex items-center gap-1.5 text-sm text-muted-foreground">
        Network guard: not checked yet.
        <InfoGlyph label="About the network guard">Dom checks it from inside the sandbox before each task.</InfoGlyph>
      </p>
    {/if}
    <p class="text-body">
      {data.tasks.length} {data.tasks.length === 1 ? "task" : "tasks"}{planned ? `, ${planned} planned or running` : ""}.
      <a href="/app/work" class="font-medium text-signal-strong underline-offset-4 hover:underline">See them in Tasks</a>
    </p>
  </section>

  <section aria-labelledby="own-skills" class="mt-10 grid gap-4">
    <h2 id="own-skills" class="text-title font-semibold">Skills</h2>
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
