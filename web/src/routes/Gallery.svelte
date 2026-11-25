<script lang="ts">
  import Activity from "@lucide/svelte/icons/activity";
  import Check from "@lucide/svelte/icons/check";
  import Plus from "@lucide/svelte/icons/plus";
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";
  import Orbit, { ACTIVITY_LABELS, type Activity as OrbitActivity } from "$lib/components/Orbit.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import * as AlertDialog from "$lib/components/ui/alert-dialog";
  import { Badge } from "$lib/components/ui/badge";
  import { Button, type ButtonVariant } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Checkbox } from "$lib/components/ui/checkbox";
  import { Input } from "$lib/components/ui/input";
  import { Label } from "$lib/components/ui/label";
  import { Progress } from "$lib/components/ui/progress";
  import * as Select from "$lib/components/ui/select";
  import { Separator } from "$lib/components/ui/separator";
  import { Slider } from "$lib/components/ui/slider";
  import { Switch } from "$lib/components/ui/switch";
  import * as Tabs from "$lib/components/ui/tabs";
  import * as Tooltip from "$lib/components/ui/tooltip";
  import { theme } from "$lib/theme.svelte";
  import { toast } from "$lib/toast.svelte";
  import Pencil from "@lucide/svelte/icons/pencil";
  import RotateCcw from "@lucide/svelte/icons/rotate-ccw";
  import Trash from "@lucide/svelte/icons/trash";
  import AppPicker from "$lib/components/kit/AppPicker.svelte";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import StatusGlyph from "$lib/components/kit/StatusGlyph.svelte";
  import StripList from "$lib/components/kit/StripList.svelte";
  import { STATUS, type Status } from "$lib/components/kit/status";
  import { chats, NOW, olderTasks, tasks as seedTasks, turns, type MockTask, type MockTurn } from "./gallery-fixtures";

  // Every component the new dashboard uses, in every state the kit shows. It stands in for a Storybook:
  // it runs inside the real app, so it is checked under Dom's real CSP (see web/README.md).
  const variants: { variant: ButtonVariant; label: string; text: string }[] = [
    { variant: "default", label: "Primary", text: "Save" },
    { variant: "secondary", label: "Secondary", text: "Test" },
    { variant: "ghost", label: "Ghost", text: "Review" },
    { variant: "outline", label: "Outline", text: "Cancel" },
    { variant: "destructive", label: "Destructive", text: "Delete" },
    { variant: "destructive-solid", label: "Final confirm", text: "Delete" },
  ];
  const providers = [
    { value: "homelab", label: "Homelab" },
    { value: "openrouter", label: "OpenRouter" },
    { value: "anthropic", label: "Anthropic" },
  ];
  const activities: OrbitActivity[] = ["idle", "replying", "desktop", "asleep"];
  const tokens = [
    ["Canvas", "bg-canvas", "--canvas"],
    ["Surface", "bg-surface", "--surface"],
    ["Raised", "bg-raised", "--raised"],
    ["Signal", "bg-signal", "--signal"],
    ["Tint", "bg-tint", "--tint"],
    ["Coral", "bg-coral", "--coral"],
    ["Amber", "bg-amber", "--amber"],
    ["Line", "bg-line", "--line"],
  ] as const;

  let provider = $state("homelab");
  let leaks = $state(true);
  let proposals = $state(false);
  let warmth = $state(65);
  let selected = $state(true);
  let unselected = $state(false);
  // The baseline (v0.8.4 W4): a mock Work list and Turn log on the strip list, with fixture data only.
  let tasks = $state<MockTask[]>([...seedTasks]);
  let pages = $state(0);
  let task = $state<string | null>(null);
  let turn = $state<string | null>(null);
  let chat = $state("");
  const shownTurns = $derived(chat ? turns.filter((item) => item.chat === chat) : turns);
  const chatName = (id: string) => chats.find((item) => item.value === id)?.label ?? id;
  const statuses = Object.keys(STATUS) as Status[];
  async function loadOlder() {
    await new Promise((resolve) => setTimeout(resolve, 150));
    tasks = [...tasks, ...olderTasks(pages * 4)];
    pages += 1;
  }
  function remove(item: MockTask) {
    tasks = tasks.filter((other) => other.id !== item.id);
    if (task === item.id) task = null;
    toast.show("ok", `Moved "${item.title}" to the trash (a demo: nothing changed)`);
  }
  const surfaces = [
    ["Canvas", "bg-canvas", "The page"],
    ["Rail", "bg-rail", "The sidebar"],
    ["Surface", "bg-surface", "Cards and lists"],
    ["Raised", "bg-raised", "Fields, hover, inset blocks"],
    ["Overlay", "bg-overlay", "Details panel, sheets"],
  ] as const;
  const scale = [
    ["Display", "text-display", "2.125 rem"],
    ["Heading", "text-heading", "1.5 rem"],
    ["Title", "text-title", "1.125 rem"],
    ["Body", "text-body", "0.9375 rem"],
    ["Meta", "text-meta", "0.8125 rem"],
    ["Caption", "text-caption", "0.75 rem"],
  ] as const;

  const showToast = (tone: "ok" | "error", text: string) => toast.show(tone, text);
</script>

<PageHeader
  kicker="Quiet orbit / Design system"
  title="Component library"
  subtitle="Every component the new dashboard is built from, in its states. Switch themes to see both."
>
  {#snippet aside()}
    <Badge variant="secondary">{theme.value} theme</Badge>
  {/snippet}
</PageHeader>

<section aria-labelledby="g-baseline" class="mb-10">
  <h2 id="g-baseline" class="text-heading font-semibold tracking-tight">Baseline</h2>
  <p class="mt-1 max-w-[60ch] text-body text-muted-foreground">
    The pieces list pages are rebuilt from. Pick a strip to open its details: beside the list on a wide screen, in a sheet on a phone.
  </p>

  <div class="mt-6 grid gap-8">
    <section aria-labelledby="g-work" class="grid gap-3">
      <div class="flex items-center gap-1.5">
        <h3 id="g-work" class="text-title font-semibold">Mock Work list</h3>
        <InfoGlyph label="About this list">Desktop tasks and Dom's own work, newest first. Scroll to the end to load older ones.</InfoGlyph>
      </div>
      <StripList
        items={tasks}
        key={(item) => item.id}
        strip={(item) => ({ status: item.status, title: item.title, at: item.at, meta: item.kind })}
        label="Tasks, newest first"
        bind:selected={task}
        hasMore={pages < 1}
        onmore={loadOlder}
        now={NOW}
      >
        {#snippet actions(item)}
          <IconButton label={`Move "${item.title}" to the trash`} icon={Trash} class="bg-transparent text-muted-foreground hover:bg-coral-tint hover:text-coral" onclick={() => remove(item)} />
        {/snippet}
        {#snippet details(item)}
          {#if item.note}<p class="mb-3 rounded-lg border border-status-waiting/40 bg-raised px-3 py-2 text-body">{item.note}</p>{/if}
          <h4 class="text-meta font-medium text-muted-foreground">Steps</h4>
          <ol class="mt-1.5 grid gap-1 text-body">
            {#each item.steps as step, index (index)}<li class="flex gap-2"><span class="text-muted-foreground tabular-nums">{index + 1}.</span>{step}</li>{/each}
          </ol>
          <div class="mt-4 flex flex-wrap gap-2">
            {#if item.status === "failed"}<Button size="sm" variant="secondary"><RotateCcw data-icon="inline-start" aria-hidden="true" /> Try again</Button>{/if}
            <Button size="sm" variant="destructive" onclick={() => remove(item)}><Trash data-icon="inline-start" aria-hidden="true" /> Move to trash</Button>
          </div>
        {/snippet}
        {#snippet empty()}<StatusNote title="Nothing on the desk" detail="Ask Dom to do something on the desktop." />{/snippet}
      </StripList>
    </section>

    <section aria-labelledby="g-turns" class="grid gap-3">
      <div class="flex flex-wrap items-end justify-between gap-3">
        <div class="flex items-center gap-1.5">
          <h3 id="g-turns" class="text-title font-semibold">Mock Turn log</h3>
          <InfoGlyph label="About the turn log">Every turn Dom took: what he did, how long it took and which model ran.</InfoGlyph>
        </div>
        <AppPicker label="Chat" options={chats} bind:value={chat} class="w-64" />
      </div>
      <StripList
        items={shownTurns}
        key={(item: MockTurn) => item.id}
        strip={(item: MockTurn) => ({ status: item.status, title: item.title, at: item.at, meta: chatName(item.chat) })}
        label="Turns, newest first"
        bind:selected={turn}
        now={NOW}
      >
        {#snippet details(item: MockTurn)}
          <dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-body">
            <dt class="text-muted-foreground">Chat</dt><dd>{chatName(item.chat)}</dd>
            <dt class="text-muted-foreground">Model</dt><dd class="font-mono text-meta break-all">{item.model}</dd>
            <dt class="text-muted-foreground">Took</dt><dd class="tabular-nums">{item.took}</dd>
            <dt class="text-muted-foreground">Calls</dt>
            <dd class="flex flex-wrap gap-1">{#each item.calls as call (call)}<Badge variant="outline">{call}</Badge>{:else}<span class="text-muted-foreground">None</span>{/each}</dd>
          </dl>
        {/snippet}
        {#snippet empty()}<StatusNote title="No turns for this chat" detail="Dom replies when someone messages him." />{/snippet}
      </StripList>
    </section>

    <div class="grid gap-4 md:grid-cols-2">
      <Card.Root aria-labelledby="g-status">
        <Card.Header><Card.Title id="g-status" class="text-lg">Status glyphs</Card.Title></Card.Header>
        <Card.Content>
          <ul class="grid grid-cols-2 gap-x-4 gap-y-2.5">
            {#each statuses as status (status)}<li><StatusGlyph {status} word /></li>{/each}
          </ul>
          <p class="mt-4 text-meta text-muted-foreground">Each status has its own shape, so colour is never the only cue. Running turns slowly, and holds still under reduced motion.</p>
        </Card.Content>
      </Card.Root>

      <Card.Root aria-labelledby="g-icons">
        <Card.Header><Card.Title id="g-icons" class="text-lg">Icon buttons and the glyph rule</Card.Title></Card.Header>
        <Card.Content class="flex flex-col gap-4">
          <div class="flex items-center gap-1">
            <IconButton label="Edit reminder" icon={Pencil} />
            <IconButton label="Try again" icon={RotateCcw} />
            <IconButton label="Move to trash" icon={Trash} class="hover:text-coral" />
            <IconButton label="Add provider" icon={Plus} variant="outline" size="icon" shortcut="N" />
          </div>
          <p class="text-meta text-muted-foreground">An icon button needs a label: it names the button and shows as its tooltip on hover and focus.</p>
          <Separator />
          <div class="flex items-center gap-1.5 text-body">
            Leak check
            <InfoGlyph label="About the leak check">Before a reply goes out, a second model reads it for anything from another chat.</InfoGlyph>
          </div>
          <p class="flex items-start gap-2 rounded-lg border border-coral/45 bg-coral-tint px-3 py-2 text-body">
            <TriangleAlert class="mt-0.5 size-4 shrink-0 text-coral" aria-hidden="true" />
            This provider keeps your messages. Pick a zero-retention one for private chats.
          </p>
          <p class="text-meta text-muted-foreground">Descriptions fold into an info glyph. Warnings stay as words.</p>
        </Card.Content>
      </Card.Root>

      <Card.Root aria-labelledby="g-surfaces">
        <Card.Header><Card.Title id="g-surfaces" class="text-lg">Surface levels</Card.Title></Card.Header>
        <Card.Content>
          <ol class="grid gap-2">
            {#each surfaces as [name, swatch, use] (name)}
              <li class="flex items-center gap-3 text-body">
                <span class="h-7 w-10 shrink-0 rounded-md border {swatch}" aria-hidden="true"></span>
                <span class="w-16 font-medium">{name}</span>
                <span class="text-meta text-muted-foreground">{use}</span>
              </li>
            {/each}
          </ol>
        </Card.Content>
      </Card.Root>

      <Card.Root aria-labelledby="g-scale">
        <Card.Header><Card.Title id="g-scale" class="text-lg">Type scale</Card.Title></Card.Header>
        <Card.Content>
          <ul class="grid gap-1.5">
            {#each scale as [name, size, rem] (name)}
              <li class="flex items-baseline gap-3">
                <span class="min-w-0 flex-1 truncate font-semibold tracking-tight {size}">{name}</span>
                <span class="shrink-0 text-caption text-muted-foreground tabular-nums">{rem}</span>
              </li>
            {/each}
          </ul>
          <p class="mt-4 text-meta text-muted-foreground">Spacing steps 4, 8, 12, 16, 24, 32 and 48 px. Motion: 120, 220 and 360 ms on one calm curve; none under reduced motion.</p>
        </Card.Content>
      </Card.Root>
    </div>
  </div>
</section>

<h2 class="mb-4 text-heading font-semibold tracking-tight">Components</h2>
<div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
  <Card.Root class="md:col-span-2" aria-labelledby="g-buttons">
    <Card.Header>
      <Card.Title id="g-buttons" class="text-lg">Buttons</Card.Title>
      <Card.Description>Secondary and ghost stay visible at rest. Solid coral is only the final confirmation.</Card.Description>
    </Card.Header>
    <Card.Content>
      <div class="grid grid-cols-[6.5rem_1fr_1fr] items-center gap-x-3 gap-y-2.5 text-sm">
        <span></span><span class="kicker">Default</span><span class="kicker">Disabled</span>
        {#each variants as { variant, label, text } (label)}
          <span class="text-muted-foreground">{label}</span>
          <span><Button {variant}>{text}</Button></span>
          <span><Button {variant} disabled>{text}</Button></span>
        {/each}
      </div>
      <Separator class="my-4" />
      <div class="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="secondary">Small</Button>
        <Button variant="secondary"><Activity data-icon="inline-start" aria-hidden="true" /> With icon</Button>
        <Button size="lg">Large</Button>
        <Button size="icon" variant="outline" aria-label="Add"><Plus /></Button>
        <Button variant="link">Link</Button>
      </div>
    </Card.Content>
  </Card.Root>

  <Card.Root aria-labelledby="g-orbit">
    <Card.Header>
      <Card.Title id="g-orbit" class="text-lg">Presence orbit</Card.Title>
      <Card.Description>Follows what Dom is doing. Still under reduced motion.</Card.Description>
    </Card.Header>
    <Card.Content class="grid grid-cols-2 gap-3">
      {#each activities as activity (activity)}
        <figure class="flex flex-col items-center gap-2 rounded-xl border bg-raised/50 p-3">
          <Orbit {activity} size="md" />
          <figcaption class="text-center text-sm">{ACTIVITY_LABELS[activity]}</figcaption>
        </figure>
      {/each}
    </Card.Content>
  </Card.Root>

  <Card.Root aria-labelledby="g-fields">
    <Card.Header><Card.Title id="g-fields" class="text-lg">Fields</Card.Title></Card.Header>
    <Card.Content class="flex flex-col gap-4">
      <div class="grid gap-1.5">
        <Label for="g-name">Provider name</Label>
        <Input id="g-name" placeholder="e.g. Homelab" />
      </div>
      <div class="grid gap-1.5">
        <Label for="g-model">Model ID</Label>
        <Input id="g-model" value="example/chat-model-7b" class="font-mono" />
      </div>
      <div class="grid gap-1.5">
        <Label for="g-endpoint">Endpoint</Label>
        <Input id="g-endpoint" value="not a url" aria-invalid="true" aria-describedby="g-endpoint-error" />
        <p id="g-endpoint-error" class="text-sm text-coral">Enter a full address, like https://example.org/v1.</p>
      </div>
      <div class="grid gap-1.5">
        <Label for="g-off">Disabled</Label>
        <Input id="g-off" value="Set by the config file" disabled />
      </div>
    </Card.Content>
  </Card.Root>

  <Card.Root aria-labelledby="g-select">
    <Card.Header><Card.Title id="g-select" class="text-lg">Select and tabs</Card.Title></Card.Header>
    <Card.Content class="flex flex-col gap-5">
      <div class="grid gap-1.5">
        <Label id="g-provider-label">Provider</Label>
        <Select.Root type="single" bind:value={provider}>
          <Select.Trigger class="w-full" aria-labelledby="g-provider-label">
            {providers.find((item) => item.value === provider)?.label}
          </Select.Trigger>
          <Select.Content>
            {#each providers as item (item.value)}
              <Select.Item value={item.value} label={item.label} />
            {/each}
          </Select.Content>
        </Select.Root>
      </div>
      <Tabs.Root value="models">
        <Tabs.List class="w-full">
          <Tabs.Trigger value="models">Models</Tabs.Trigger>
          <Tabs.Trigger value="providers">Providers</Tabs.Trigger>
          <Tabs.Trigger value="permissions">Permissions</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="models" class="pt-2 text-sm text-muted-foreground">Which model runs each role.</Tabs.Content>
        <Tabs.Content value="providers" class="pt-2 text-sm text-muted-foreground">Where the models are served from.</Tabs.Content>
        <Tabs.Content value="permissions" class="pt-2 text-sm text-muted-foreground">What Dom may do without asking.</Tabs.Content>
      </Tabs.Root>
    </Card.Content>
  </Card.Root>

  <Card.Root aria-labelledby="g-controls">
    <Card.Header><Card.Title id="g-controls" class="text-lg">Selection controls</Card.Title></Card.Header>
    <Card.Content class="flex flex-col gap-4">
      <div class="flex items-center justify-between gap-3">
        <Label for="g-proposals">Enable memory proposals</Label>
        <Switch id="g-proposals" bind:checked={proposals} />
      </div>
      <div class="flex items-center justify-between gap-3">
        <Label for="g-leaks">Check replies for leaks</Label>
        <Switch id="g-leaks" bind:checked={leaks} />
      </div>
      <div class="grid gap-2.5">
        <div class="flex items-center justify-between">
          <Label id="g-warmth-label">Warmth</Label>
          <span class="font-mono text-sm text-signal-strong">{(warmth / 100).toFixed(2)}</span>
        </div>
        <Slider type="single" bind:value={warmth} max={100} step={5} thumbLabelledby="g-warmth-label" />
      </div>
      <div class="flex items-center gap-2.5">
        <Checkbox id="g-selected" bind:checked={selected} />
        <Label for="g-selected">Selected</Label>
      </div>
      <div class="flex items-center gap-2.5">
        <Checkbox id="g-unselected" bind:checked={unselected} />
        <Label for="g-unselected">Unselected</Label>
      </div>
    </Card.Content>
  </Card.Root>

  <Card.Root aria-labelledby="g-badges">
    <Card.Header><Card.Title id="g-badges" class="text-lg">Badges and telemetry</Card.Title></Card.Header>
    <Card.Content class="flex flex-col gap-4">
      <div class="flex flex-wrap gap-2">
        <Badge>Local</Badge>
        <Badge variant="outline">Cloud</Badge>
        <Badge variant="secondary"><Check aria-hidden="true" /> Key set</Badge>
        <Badge variant="destructive">Key missing</Badge>
        <Badge>OK · 0.8 s</Badge>
      </div>
      <div class="rounded-xl border bg-raised/40 p-4">
        <div class="flex items-center justify-between gap-2">
          <p class="font-medium">Homelab</p>
          <Badge>Local</Badge>
        </div>
        <p class="mt-1.5 font-mono text-sm text-muted-foreground">https://models.example.org/v1</p>
        <div class="mt-3 flex items-center gap-3">
          <Progress value={42} max={100} aria-label="Context used" class="flex-1" />
          <span class="font-mono text-xs text-muted-foreground">42%</span>
        </div>
      </div>
      <Tooltip.Root>
        <Tooltip.Trigger>
          {#snippet child({ props })}
            <Button {...props} variant="outline" size="sm">Hover for a tooltip</Button>
          {/snippet}
        </Tooltip.Trigger>
        <Tooltip.Content>Tooltips position themselves under the CSP.</Tooltip.Content>
      </Tooltip.Root>
    </Card.Content>
  </Card.Root>

  <Card.Root aria-labelledby="g-dialog">
    <Card.Header><Card.Title id="g-dialog" class="text-lg">Confirm dialog</Card.Title></Card.Header>
    <Card.Content class="flex flex-col gap-4">
      <div class="rounded-xl border bg-raised/40 p-4" aria-hidden="true">
        <p class="font-medium">Delete 3 reminders?</p>
        <p class="mt-1 text-sm text-muted-foreground">They stay in the archive for 30 days.</p>
        <div class="mt-4 flex gap-2">
          <Button variant="outline" size="sm" tabindex={-1}>Cancel</Button>
          <Button variant="destructive-solid" size="sm" tabindex={-1}>Delete</Button>
        </div>
      </div>
      <AlertDialog.Root>
        <AlertDialog.Trigger>
          {#snippet child({ props })}
            <Button {...props} variant="destructive">Delete reminders…</Button>
          {/snippet}
        </AlertDialog.Trigger>
        <AlertDialog.Content>
          <AlertDialog.Header>
            <AlertDialog.Title>Delete 3 reminders?</AlertDialog.Title>
            <AlertDialog.Description>They stay in the archive for 30 days.</AlertDialog.Description>
          </AlertDialog.Header>
          <AlertDialog.Footer>
            <AlertDialog.Cancel variant="outline">Cancel</AlertDialog.Cancel>
            <AlertDialog.Action variant="destructive-solid" onclick={() => showToast("ok", "Deleted (a demo: nothing changed)")}>Delete</AlertDialog.Action>
          </AlertDialog.Footer>
        </AlertDialog.Content>
      </AlertDialog.Root>
    </Card.Content>
  </Card.Root>

  <Card.Root aria-labelledby="g-toasts">
    <Card.Header><Card.Title id="g-toasts" class="text-lg">Toasts</Card.Title></Card.Header>
    <Card.Content class="flex flex-col gap-3">
      <p class="flex items-center gap-2.5 rounded-xl border px-3.5 py-2.5 text-sm"><Check class="size-4 text-signal" aria-hidden="true" /> Changes saved</p>
      <p class="flex items-center gap-2.5 rounded-xl border border-coral/45 bg-coral-tint px-3.5 py-2.5 text-sm"><TriangleAlert class="size-4 text-coral" aria-hidden="true" /> Couldn't connect</p>
      <div class="flex flex-wrap gap-2">
        <Button variant="secondary" size="sm" onclick={() => showToast("ok", "Changes saved")}>Show saved</Button>
        <Button variant="ghost" size="sm" onclick={() => showToast("error", "Couldn't connect")}>Show error</Button>
      </div>
      <p class="kicker">Polite live region</p>
    </Card.Content>
  </Card.Root>

  <Card.Root aria-labelledby="g-empty">
    <Card.Header><Card.Title id="g-empty" class="text-lg">Empty and error states</Card.Title></Card.Header>
    <Card.Content class="flex flex-col gap-3">
      <StatusNote title="No reminders yet" detail="Ask Dom to remember something for you.">
        <Button variant="secondary" size="sm"><Plus data-icon="inline-start" aria-hidden="true" /> Add reminder</Button>
      </StatusNote>
      <StatusNote tone="error" title="Couldn't connect" detail="Homelab is not answering. Check the endpoint and try again.">
        <Button variant="secondary" size="sm">Retry</Button>
      </StatusNote>
    </Card.Content>
  </Card.Root>

  <Card.Root aria-labelledby="g-type">
    <Card.Header><Card.Title id="g-type" class="text-lg">Type and colour</Card.Title></Card.Header>
    <Card.Content class="flex flex-col gap-4">
      <div>
        <p class="text-2xl font-semibold tracking-tight">Geist, for words</p>
        <p class="mt-1 font-mono text-sm text-muted-foreground">Geist Mono · model IDs, 0.8 s, $0.42</p>
      </div>
      <ul class="grid gap-y-2">
        {#each tokens as [name, swatch, variable] (name)}
          <li class="flex items-center gap-2.5 text-sm">
            <span class="size-5 rounded-md border {swatch}" aria-hidden="true"></span>
            <span class="flex-1">{name}</span>
            <span class="font-mono text-xs text-muted-foreground">{variable}</span>
          </li>
        {/each}
      </ul>
    </Card.Content>
  </Card.Root>
</div>
