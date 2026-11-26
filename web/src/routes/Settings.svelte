<script lang="ts">
  import Activity from "@lucide/svelte/icons/activity";
  import ArrowDown from "@lucide/svelte/icons/arrow-down";
  import ArrowUp from "@lucide/svelte/icons/arrow-up";
  import Cloud from "@lucide/svelte/icons/cloud";
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import Pencil from "@lucide/svelte/icons/pencil";
  import Plus from "@lucide/svelte/icons/plus";
  import SearchIcon from "@lucide/svelte/icons/search";
  import Server from "@lucide/svelte/icons/server";
  import Trash from "@lucide/svelte/icons/trash";
  import { onMount } from "svelte";
  import {
    getJSON,
    send,
    type ModelSettings,
    type Permission,
    type Permissions,
    type ProfileInterval,
    type ProviderSetup,
    type ProviderView,
    type SettingsRole,
    type StudioChain,
    type StudioFallback,
    type TestResult,
  } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import DesktopSteps from "$lib/components/settings/DesktopSteps.svelte";
  import LeakScan from "$lib/components/settings/LeakScan.svelte";
  import MeaningIndex from "$lib/components/settings/MeaningIndex.svelte";
  import MediaInputs from "$lib/components/settings/MediaInputs.svelte";
  import Services from "$lib/components/settings/Services.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import RestartBanner from "$lib/components/system/RestartBanner.svelte";
  import MediaLimits from "$lib/components/settings/MediaLimits.svelte";
  import { forgetLists } from "$lib/components/settings/modelLists.svelte";
  import ProviderEditor from "$lib/components/settings/ProviderEditor.svelte";
  import RoutePrivacy from "$lib/components/settings/RoutePrivacy.svelte";
  import RoleEditor from "$lib/components/settings/RoleEditor.svelte";
  import {
    ACCOUNT_ZDR_NOTE, DECISION_ROLES, KIND_INFO, POLICY_INFO, ROLE_INFO, ROLES, TYPE_INFO, TYPES, ZDR_RECOMMENDATION, host, fitsRole, isDeepSeekChat, seconds, slotLabel, useLabel,
  } from "$lib/components/settings/roles";
  import StudioBackupEditor from "$lib/components/settings/StudioBackupEditor.svelte";
  import { CHAIN_INFO, CHAINS, STUDIO_PROVIDERS } from "$lib/components/settings/studio";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import * as AlertDialog from "$lib/components/ui/alert-dialog";
  import { Badge } from "$lib/components/ui/badge";
  import * as Select from "$lib/components/ui/select";
  import { Button } from "$lib/components/ui/button";
  import { Input } from "$lib/components/ui/input";
  import * as Card from "$lib/components/ui/card";
  import { Switch } from "$lib/components/ui/switch";
  import * as Tabs from "$lib/components/ui/tabs";
  import type { Destination, Tab } from "$lib/nav";
  import { toast } from "$lib/toast.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  let models = $state<ModelSettings | null>(null);
  let permissions = $state<Permission[]>([]);
  let desktopSteps = $state<Permissions["desktop_steps"] | null>(null);
  let profileEvery = $state<ProfileInterval>(14);
  // One revision for the whole page: both GETs hash the same config.toml, and every save moves it.
  let revision = $state("");
  let failed = $state("");
  let section = $state("models");
  // One edit at a time across both tabs: each save takes the revision the last one returned.
  type Editing = { role: SettingsRole; adding?: boolean } | { provider: ProviderSetup | null } | { studio: StudioChain; index: number | null };
  let editing = $state<Editing | null>(null);
  let reordering = $state(""); // the role whose fallback is moving up, which shows its progress
  let tests = $state<Record<string, TestResult | "running">>({});
  let pending = $state<Permission | null>(null); // a switch waiting on its confirm dialog
  let removing = $state<ProviderSetup | null>(null); // a provider waiting on its remove dialog
  let removingBackup = $state<{ chain: StudioChain; index: number; item: StudioFallback } | null>(null);
  // One permission save at a time: each takes the revision the last one returned.
  let savingPermission = $state(false);
  let savingId = $state(""); // the switch being saved, which shows its progress
  let servicesRestart = $state(false); // a saved service (desktop hosts, T3) waits for a restart

  async function load() {
    try {
      const [loadedModels, loadedPermissions] = await Promise.all([
        getJSON<ModelSettings>("/api/models"),
        getJSON<Permissions>("/api/permissions"),
      ]);
      models = loadedModels;
      permissions = loadedPermissions.items;
      desktopSteps = loadedPermissions.desktop_steps;
      profileEvery = loadedPermissions.profile_photo_every_days;
      revision = loadedModels.revision;
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(load);

  function saved(result: ModelSettings, message = "Saved.") {
    models = result;
    revision = result.revision;
    editing = null;
    tests = {};
    toast.show("ok", result.restart_required ? `${message} Restart Dom to apply.` : message);
  }

  function providerSaved(result: ModelSettings) {
    forgetLists(); // an address may have moved; the next picker fetches again
    saved(result, "Provider saved.");
  }

  const editingLabel = $derived(
    editing === null ? "" : "role" in editing ? ROLE_INFO[editing.role].label
      : "studio" in editing ? `the ${CHAIN_INFO[editing.studio].label.toLowerCase()} backup`
      : (editing.provider?.name ?? "the new provider"),
  );
  const isEditingStudio = (chain: StudioChain, index: number | null) =>
    editing !== null && "studio" in editing && editing.studio === chain && editing.index === index;
  const isEditingRole = (role: SettingsRole) => editing !== null && "role" in editing && editing.role === role;
  const isEditingProvider = (name: string | null) =>
    editing !== null && "provider" in editing && (editing.provider?.name ?? null) === name;

  /** Test a saved slot (index), or the whole saved search chain (null). */
  async function test(role: SettingsRole, index: number | null) {
    const key = `${role}:${index}`;
    tests[key] = "running";
    try {
      tests[key] = await send<TestResult>("/api/models/test", { revision, role, index });
    } catch {
      delete tests[key];
    }
  }

  async function testProvider(provider: ProviderSetup) {
    const key = `provider:${provider.name}`;
    tests[key] = "running";
    try {
      tests[key] = await send<TestResult>("/api/providers/test", { revision, name: provider.name });
    } catch {
      delete tests[key];
    }
  }

  /** A saved slot as the save route takes it back. */
  function slotBody(settings: ModelSettings, role: SettingsRole, view: ProviderView) {
    if (role === "search") {
      const needsModel = settings.search_providers.find((item) => item.id === view.provider)?.needs_model ?? false;
      return { provider: view.provider, base_url: view.base_url, model: needsModel ? view.model : "", api_key_env: view.api_key_env };
    }
    if (role === "triage") return { base_url: view.base_url, model: view.model, api_key_env: view.api_key_env };
    return { base_url: view.base_url, model: view.model, api_key_env: view.api_key_env, reasoning_effort: view.reasoning_effort };
  }

  /** Move a fallback one place up; the first fallback becomes the primary. Moved slots are not tested again. */
  async function moveUp(settings: ModelSettings, role: SettingsRole, index: number) {
    const route = settings.roles[role];
    const chain = [route.primary!, ...route.fallbacks].map((view) => slotBody(settings, role, view));
    [chain[index - 1], chain[index]] = [chain[index], chain[index - 1]];
    reordering = role;
    try {
      const [primary, ...fallbacks] = chain;
      saved(await send<ModelSettings>("/api/models", { revision, role, primary, fallbacks }), `${ROLE_INFO[role].label}: order saved.`);
    } catch {
      // the toast says why; the saved order stands
    } finally {
      reordering = "";
    }
  }

  /** Move a studio backup one place up. It applies at once and tests nothing: no new provider is reached. */
  async function moveStudio(chain: StudioChain, index: number) {
    reordering = `studio-${chain}`;
    try {
      saved(await send<ModelSettings>("/api/studio/move", { revision, chain, index }), `${CHAIN_INFO[chain].label}: order saved.`);
    } catch {
      // the toast says why
    } finally {
      reordering = "";
    }
  }

  async function removeStudio(chain: StudioChain, index: number) {
    removingBackup = null;
    reordering = `studio-${chain}`;
    try {
      saved(await send<ModelSettings>("/api/studio/remove", { revision, chain, index }), `${CHAIN_INFO[chain].label}: backup removed.`);
    } catch {
      // the toast says why
    } finally {
      reordering = "";
    }
  }

  async function testStudio(chain: StudioChain, index: number) {
    const key = `studio:${chain}:${index}`;
    tests[key] = "running";
    try {
      tests[key] = await send<TestResult>("/api/studio/test", { revision, chain, index });
    } catch {
      delete tests[key];
    }
  }

  const INTERVALS: { days: ProfileInterval; label: string }[] = [
    { days: 1, label: "Day" }, { days: 7, label: "Week" }, { days: 14, label: "2 weeks" }, { days: 30, label: "Month" },
  ];
  let savingInterval = $state(false);

  async function saveInterval(days: ProfileInterval) {
    if (savingInterval || days === profileEvery) return;
    const before = profileEvery;
    profileEvery = days;
    savingInterval = true;
    try {
      const result = await send<Permissions>("/api/permissions/profile-interval", { revision, days });
      permissions = result.items;
      revision = result.revision;
      profileEvery = result.profile_photo_every_days;
      toast.show("ok", `The profile photo now changes at most every ${INTERVALS.find((item) => item.days === days)!.label.toLowerCase()}.`);
    } catch {
      profileEvery = before;
    } finally {
      savingInterval = false;
    }
  }

  async function removeProvider(provider: ProviderSetup) {
    removing = null;
    try {
      saved(await send<ModelSettings>("/api/providers/remove", { revision, name: provider.name }), `${provider.name} removed.`);
    } catch {
      // the toast says why
    }
  }

  // Optimistic: a switch has no content to reject, so it flips at once and flips back if the save fails.
  async function setPermission(item: Permission, enabled: boolean) {
    pending = null;
    item.enabled = enabled;
    savingPermission = true;
    savingId = item.id;
    try {
      const result = await send<Permissions>("/api/permissions", { revision, id: item.id, enabled });
      permissions = result.items;
      revision = result.revision;
      toast.show("ok", `${item.label}: ${enabled ? "on" : "off"}.`);
    } catch {
      item.enabled = !enabled;
    } finally {
      savingPermission = false;
      savingId = "";
    }
  }

  function flip(item: Permission, enabled: boolean) {
    if (savingPermission) return;
    // Turning the leak check off lets Dom send without the privacy screen: ask first.
    if (item.id === "leak_check" && !enabled) pending = item;
    else setPermission(item, enabled);
  }

  // "What Dom may do": the switches in sections, each with the panels that belong to it, narrowed by the search box.
  type SectionId = "messaging" | "media" | "desktop" | "memory" | "safety";
  const SECTIONS: { id: SectionId; label: string; ids: string[]; words: string }[] = [
    { id: "messaging", label: "Messaging", ids: ["openers", "openers_friends", "openers_groups", "ephemeral_replies"], words: "" },
    {
      id: "media", label: "Media", ids: ["studio_voice", "studio_video", "studio_images", "studio_special", "studio_profile"],
      words: "media limits voice notes video photos gap per day week left spend",
    },
    { id: "desktop", label: "Desktop", ids: [], words: "desktop tasks steps step budget computer" },
    { id: "memory", label: "Memory", ids: ["memory_self_edit"], words: "" },
    { id: "safety", label: "Safety", ids: ["leak_check"], words: "leak scan level strict standard relaxed off private" },
  ];
  let permissionQuery = $state("");
  const matches = (text: string, query: string) => query.split(/\s+/).every((word) => text.toLowerCase().includes(word));
  const visibleSections = $derived.by(() => {
    const query = permissionQuery.trim().toLowerCase();
    const placed = new Set(SECTIONS.flatMap((group) => group.ids));
    return SECTIONS.map((group) => {
      // A switch the page doesn't know yet still shows, under Messaging, rather than vanishing.
      const own = permissions.filter((item) => group.ids.includes(item.id) || (group.id === "messaging" && !placed.has(item.id)));
      const items = query ? own.filter((item) => matches(`${group.label} ${item.label} ${item.description}`, query)) : own;
      const extras = group.words !== "" && (!query || matches(`${group.label} ${group.words}`, query));
      return { ...group, items, extras };
    }).filter((group) => group.items.length || group.extras);
  });

  async function reloadPermissions() {
    try {
      const loaded = await getJSON<Permissions>("/api/permissions");
      permissions = loaded.items;
      revision = loaded.revision;
    } catch {
      // the switches stay as they were; the next save says if the file moved
    }
  }

  type KeyState = "none" | "set" | "missing";
  const keyState = (variable: string | null, set: boolean): KeyState => (!variable ? "none" : set ? "set" : "missing");
  const slotName = (settings: ModelSettings, view: ProviderView) =>
    view.model || settings.search_providers.find((item) => item.id === view.provider)?.label;
</script>

{#snippet keyBadge(state: KeyState)}
  {#if state === "none"}
    <Badge variant="outline">No key needed</Badge>
  {:else if state === "set"}
    <Badge variant="secondary">Key set</Badge>
  {:else}
    <Badge variant="destructive">Key missing</Badge>
  {/if}
{/snippet}

{#snippet slotTest(role: SettingsRole, index: number, name: string)}
  <Button variant="ghost" size="xs" onclick={() => test(role, index)} disabled={tests[`${role}:${index}`] === "running"}>
    <Activity data-icon="inline-start" aria-hidden="true" /> Test <span class="sr-only">{name}</span>
  </Button>
{/snippet}

{#snippet result(key: string)}
  {@const outcome = tests[key]}
  <p class="min-h-5 text-sm empty:min-h-0" aria-live="polite">
    {#if outcome === "running"}
      <span class="text-muted-foreground">Testing…</span>
    {:else if outcome?.ok}
      <Badge>OK · {seconds(outcome.latency_ms)}</Badge>
      <span class="ml-1 text-muted-foreground">{outcome.provider ? `${outcome.provider}: ` : ""}{outcome.reply}</span>
    {:else if outcome}
      <span class="text-coral">{seconds(outcome.latency_ms)} · {outcome.error}</span>
    {/if}
  </p>
{/snippet}

{#snippet waitNote(id: string)}
  {#if editing !== null}
    <!-- One edit at a time: each save takes the revision the last one returned. -->
    <p {id} class="text-xs text-muted-foreground">Finish editing {editingLabel} first.</p>
  {/if}
{/snippet}

{#snippet permissionRow(item: Permission)}
  <li class="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-5 py-4">
    <div class="min-w-0 flex-1 basis-60">
      <span class="flex items-center gap-1">
        <label for={`perm-${item.id}`} class="font-medium">{item.label}</label>
        <InfoGlyph label={`About ${item.label}`}>{item.description}</InfoGlyph>
      </span>
      {#if !item.available}
        <p id={`perm-${item.id}-hint`} class="mt-0.5 text-sm {item.enabled ? 'text-coral' : 'text-muted-foreground'}">
          {item.enabled ? "On, but nothing can serve it: Dom sends text instead. " : ""}{item.needs}
          {#if item.id !== "studio_profile"}
            <button type="button" class="text-signal-strong underline underline-offset-2" onclick={() => (section = "models")}>
              Studio cloud backups
            </button>
          {/if}
        </p>
      {/if}
      {#if item.id === "studio_profile"}
        <div class="mt-2 flex flex-wrap items-center gap-2">
          <span id="profile-interval-label" class="text-sm">Change at most every</span>
          <Select.Root type="single" value={String(profileEvery)} onValueChange={(value) => saveInterval(Number(value) as ProfileInterval)}>
            <Select.Trigger class="h-8 w-32" aria-labelledby="profile-interval-label" disabled={savingInterval}>
              {INTERVALS.find((option) => option.days === profileEvery)?.label}
            </Select.Trigger>
            <Select.Content>
              {#each INTERVALS as option (option.days)}
                <Select.Item value={String(option.days)} label={option.label} />
              {/each}
            </Select.Content>
          </Select.Root>
          {#if savingInterval}
            <span role="status" class="flex items-center gap-1.5 text-xs text-muted-foreground">
              <LoaderCircle class="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…
            </span>
          {/if}
        </div>
      {/if}
    </div>
    <div class="flex shrink-0 items-center gap-2">
      {#if savingId === item.id}
        <span role="status" class="flex items-center gap-1.5 text-xs text-muted-foreground">
          <LoaderCircle class="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…
        </span>
      {/if}
      <Switch
        id={`perm-${item.id}`}
        aria-describedby={item.available ? undefined : `perm-${item.id}-hint`}
        aria-busy={savingId === item.id}
        bind:checked={() => item.enabled, (value) => flip(item, value)}
        disabled={savingPermission || (!item.available && !item.enabled)}
      />
    </div>
  </li>
{/snippet}

{#snippet roleCard(settings: ModelSettings, role: SettingsRole)}
  {@const info = ROLE_INFO[role]}
  {@const route = settings.roles[role]}
  {#if isEditingRole(role)}
    <RoleEditor {role} {settings} {revision} adding={editing !== null && "role" in editing && editing.adding} onsaved={(result) => saved(result)} oncancel={() => (editing = null)} />
  {:else}
    {@const inherited = role === "escalation" && !route.primary}
    {@const testKey = `${role}:${role === "search" ? null : 0}`}
    {@const busy = editing !== null || reordering !== ""}
    <Card.Root aria-labelledby={`${role}-title`}>
      <Card.Header>
        <Card.Title id={`${role}-title`} class="text-lg">{info.label}</Card.Title>
        <Card.Description>{info.summary}</Card.Description>
        <Card.Action>
          <span class="grid size-9 place-items-center rounded-lg border bg-raised text-signal" aria-hidden="true"><info.icon class="size-4" /></span>
        </Card.Action>
      </Card.Header>
      <Card.Content class="flex flex-1 flex-col gap-4">
        {#if route.primary}
          {@const primary = route.primary}
          <div class="rounded-xl border bg-raised/40 p-3.5">
            <div class="flex items-start justify-between gap-2">
              <p class="min-w-0 font-mono text-sm break-all text-signal-strong">{slotName(settings, primary)}</p>
              <Badge variant={primary.kind === "local" ? "default" : "outline"}>{primary.kind === "local" ? "Local" : "Cloud"}</Badge>
            </div>
            <p class="mt-1 text-sm break-all text-muted-foreground">
              {host(primary.base_url)}{primary.reasoning_effort ? ` · ${primary.reasoning_effort} effort` : ""}
            </p>
            <RoutePrivacy url={primary.base_url} local={primary.kind === "local"} class="mt-2" />
            {#if primary.api_key_env && !primary.key_set}
              <p class="mt-2 text-sm text-coral">
                Add {primary.api_key_env} to .env{role === "triage" ? ", then press Test. Until then triage and the leak check are off." : ""}
              </p>
            {/if}
            {#if role === "search"}
              <!-- The footer tests the whole chain; this tests the primary alone. -->
              <div class="mt-2">{@render slotTest(role, 0, `${info.label} primary`)}</div>
              {@render result(`${role}:0`)}
            {/if}
          </div>
          {#if role !== "triage"}
            <div>
              <p class="kicker">If it fails</p>
              {#if route.fallbacks.length}
                <ol class="mt-1.5 grid gap-1">
                  {#each route.fallbacks as fallback, index (index)}
                    <li class="text-sm">
                      <div class="flex items-start gap-2">
                        <span class="font-mono text-xs text-muted-foreground">{String(index + 1).padStart(2, "0")}</span>
                        <span class="min-w-0 flex-1 font-mono break-all">{slotName(settings, fallback)} · {host(fallback.base_url)}</span>
                      </div>
                      <div class="ml-6 flex flex-wrap items-center gap-1">
                        {@render slotTest(role, index + 1, `${info.label} fallback ${index + 1}`)}
                        <Button
                          variant="ghost"
                          size="xs"
                          onclick={() => moveUp(settings, role, index + 1)}
                          disabled={busy}
                          aria-describedby={editing !== null ? `${role}-wait` : undefined}
                        >
                          <ArrowUp data-icon="inline-start" aria-hidden="true" /> Move up
                          <span class="sr-only">{info.label} fallback {index + 1}{index === 0 ? ", making it the primary" : ""}</span>
                        </Button>
                      </div>
                      <RoutePrivacy url={fallback.base_url} local={fallback.kind === "local"} class="ml-6" />
                      {#if fallback.api_key_env && !fallback.key_set}<p class="ml-6 text-coral">Add {fallback.api_key_env} to .env</p>{/if}
                      <div class="ml-6">{@render result(`${role}:${index + 1}`)}</div>
                    </li>
                  {/each}
                </ol>
              {:else}
                <div class="mt-1.5 flex flex-wrap items-center justify-between gap-2">
                  <p class="text-sm text-muted-foreground">
                    {role === "chat" ? "The fallback role, if set" : role === "drafting" ? "The chat route" : "No backup"}
                  </p>
                  <Button variant="ghost" size="xs" onclick={() => (editing = { role, adding: true })} disabled={busy}>
                    <Plus data-icon="inline-start" aria-hidden="true" /> Add backup <span class="sr-only">for {info.label}</span>
                  </Button>
                </div>
              {/if}
            </div>
          {/if}
        {:else}
          <div class="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-dashed p-3.5">
            <p class="text-sm text-muted-foreground">{inherited ? "Not set: uses the drafting providers." : "Not set."}</p>
            {#if !settings.providers.some((item) => fitsRole(item, role))}
              <Button variant="ghost" size="xs" onclick={() => { section = "providers"; editing = { provider: null }; }} disabled={busy}>
                <Plus data-icon="inline-start" aria-hidden="true" /> Add provider <span class="sr-only">for {info.label}</span>
              </Button>
            {/if}
          </div>
        {/if}
        {#if info.note}<p class="text-xs text-muted-foreground">{info.note}</p>{/if}
        {#if reordering === role}
          <p role="status" class="flex items-center gap-1.5 text-xs text-muted-foreground">
            <LoaderCircle class="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…
          </p>
        {/if}
        {@render result(testKey)}
      </Card.Content>
      <Card.Footer class="flex-wrap gap-2">
        <Button variant="secondary" onclick={() => (editing = { role })} disabled={busy} aria-describedby={editing !== null ? `${role}-wait` : undefined}>
          <Pencil data-icon="inline-start" aria-hidden="true" /> {route.primary ? "Edit" : "Set up"}
          <span class="sr-only">{info.label}</span>
        </Button>
        {#if route.primary || inherited}
          <Button variant="ghost" onclick={() => test(role, role === "search" ? null : 0)} disabled={tests[testKey] === "running"}>
            <Activity data-icon="inline-start" aria-hidden="true" /> {role === "search" ? "Test chain" : "Test"}
            <span class="sr-only">{info.label}</span>
          </Button>
        {/if}
        {@render waitNote(`${role}-wait`)}
      </Card.Footer>
    </Card.Root>
  {/if}
{/snippet}

{#snippet providerCard(settings: ModelSettings, provider: ProviderSetup, n: string)}
  {@const testKey = `provider:${provider.name}`}
  <!-- A media provider's backups each set their own data policy, so the search services' notes don't apply. -->
  {@const search = provider.type === "media" ? undefined : settings.search_providers.find((item) => item.id === provider.kind)}
  {#if isEditingProvider(provider.name)}
    <ProviderEditor original={provider} {settings} {revision} onsaved={providerSaved} oncancel={() => (editing = null)} />
  {:else}
    <Card.Root aria-labelledby={`provider-${n}`}>
      <Card.Header>
        <div class="flex items-center gap-3">
          <span class="grid size-9 shrink-0 place-items-center rounded-lg border bg-raised text-signal" aria-hidden="true">
            {#if provider.is_local}<Server class="size-4" />{:else}<Cloud class="size-4" />{/if}
          </span>
          <div class="min-w-0">
            <Card.Title id={`provider-${n}`} class="text-base break-all">{provider.name}</Card.Title>
            <div class="mt-1 flex flex-wrap gap-1">
              <Badge variant="secondary">{TYPE_INFO[provider.type].label}</Badge>
              <Badge variant="outline">{KIND_INFO[provider.kind].label}</Badge>
              <Badge variant={provider.is_local ? "default" : "outline"}>{provider.is_local ? "Local" : "Cloud"}</Badge>
              {@render keyBadge(keyState(provider.api_key_env, provider.key_set))}
            </div>
          </div>
        </div>
      </Card.Header>
      <Card.Content class="flex flex-1 flex-col gap-3">
        <p class="rounded-lg bg-raised/60 px-3 py-2 font-mono text-xs break-all text-muted-foreground">
          {provider.base_url}{provider.api_key_env ? ` · ${provider.api_key_env}` : ""}
        </p>
        {#if provider.api_key_env && !provider.key_set}<p class="text-sm text-coral">Add {provider.api_key_env} to .env</p>{/if}
        {#if provider.kind === "openai" && isDeepSeekChat(provider.base_url)}
          <RoutePrivacy url={provider.base_url} />
        {/if}
        {#if search}
          <p class="text-xs text-muted-foreground">
            {search.data_retention}
            {#if search.policy_url}
              <a class="text-signal-strong underline underline-offset-2" href={search.policy_url} target="_blank" rel="noopener noreferrer">{search.policy_label ?? "Data policy"}</a>
            {/if}
          </p>
        {/if}
        <div>
          <p class="kicker">Used by</p>
          <p class="mt-1 text-sm">{provider.uses.length ? provider.uses.map(useLabel).join(" · ") : "Nothing yet"}</p>
        </div>
        {#if !provider.saved}
          <p class="text-xs text-muted-foreground">Found in config.toml's {provider.type === "media" ? "studio backups" : "roles"}. Edit to name and save it here.</p>
        {/if}
        {@render result(testKey)}
      </Card.Content>
      <Card.Footer class="flex-wrap gap-2">
        <Button variant="secondary" onclick={() => (editing = { provider })} disabled={editing !== null} aria-describedby={editing !== null ? `provider-${n}-wait` : undefined}>
          <Pencil data-icon="inline-start" aria-hidden="true" /> Edit <span class="sr-only">{provider.name}</span>
        </Button>
        <Button variant="ghost" onclick={() => testProvider(provider)} disabled={tests[testKey] === "running"}>
          <Activity data-icon="inline-start" aria-hidden="true" /> Test <span class="sr-only">{provider.name}</span>
        </Button>
        {#if provider.saved}
          <Button variant="ghost" onclick={() => (removing = provider)} disabled={editing !== null}>
            <Trash data-icon="inline-start" aria-hidden="true" /> Remove <span class="sr-only">{provider.name}</span>
          </Button>
        {/if}
        {@render waitNote(`provider-${n}-wait`)}
      </Card.Footer>
    </Card.Root>
  {/if}
{/snippet}

<PageHeader kicker="System / Configuration" title="Settings" subtitle="Fine-tune how Dom thinks and what he may do." />
<PageTabs {destination} {tab} />

{#if models === null && failed}
  <StatusNote tone="error" title="Couldn't load settings" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="secondary" onclick={load}>Retry</Button>
  </StatusNote>
{:else if models === null}
  <p class="sr-only" role="status">Loading settings…</p>
  <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3" aria-hidden="true">
    {#each [0, 1, 2] as index (index)}<div class="h-64 animate-pulse rounded-xl border bg-surface"></div>{/each}
  </div>
{:else}
  {@const settings = models}
  {#if settings.restart_required || servicesRestart}
    <RestartBanner
      title="Restart Dom to apply"
      detail={settings.restart_required ? "The running model routes differ from config.toml." : "A saved service is used only after a restart."}
      class="mb-5"
    />
  {/if}

  <Tabs.Root bind:value={section}>
    <Tabs.List variant="line" class="no-scrollbar mb-5 h-auto! w-full justify-start gap-5 overflow-x-auto border-b pb-1">
      <Tabs.Trigger value="models" class="flex-none px-0 py-2 data-active:text-signal-strong">Models</Tabs.Trigger>
      <Tabs.Trigger value="providers" class="flex-none px-0 py-2 data-active:text-signal-strong">Providers</Tabs.Trigger>
      <Tabs.Trigger value="permissions" class="flex-none px-0 py-2 data-active:text-signal-strong">What Dom may do</Tabs.Trigger>
      <Tabs.Trigger value="services" class="flex-none px-0 py-2 data-active:text-signal-strong">Services</Tabs.Trigger>
    </Tabs.List>

    <Tabs.Content value="models">
      <section aria-labelledby="routing-title" class="mb-8">
        <div class="mb-3 flex flex-wrap items-end justify-between gap-2">
          <h2 id="routing-title" class="text-lg font-semibold">Jobs</h2>
          <p class="kicker">{String(ROLES.length).padStart(2, "0")} jobs / routing</p>
        </div>
        <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {#each ROLES as role (role)}{@render roleCard(settings, role)}{/each}
        </div>
      </section>
      <section aria-labelledby="decisions-title" class="mb-8">
        <div class="mb-3 flex flex-wrap items-end justify-between gap-2">
          <h2 id="decisions-title" class="text-lg font-semibold">Decision models</h2>
          <p class="kicker">{String(DECISION_ROLES.length).padStart(2, "0")} {DECISION_ROLES.length === 1 ? "model" : "models"}</p>
        </div>
        <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {#each DECISION_ROLES as role (role)}{@render roleCard(settings, role)}{/each}
        </div>
      </section>
      <section aria-labelledby="meaning-title" class="mb-8">
        <div class="mb-3 flex flex-wrap items-end justify-between gap-2">
          <h2 id="meaning-title" class="text-lg font-semibold">Search by meaning</h2>
          <p class="kicker">Embeddings and inputs</p>
        </div>
        <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {@render roleCard(settings, "embedding")}
          <div class="grid content-start gap-4 xl:col-span-2"><MeaningIndex version={settings.revision} /></div>
          <div class="md:col-span-2 xl:col-span-3"><MediaInputs /></div>
        </div>
      </section>
      <section aria-labelledby="studio-title">
        <div class="mb-3 flex flex-wrap items-end justify-between gap-2">
          <h2 id="studio-title" class="text-lg font-semibold">Studio cloud backups</h2>
          <p class="kicker">When the local studio can't</p>
        </div>
        <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {#each CHAINS as chainName (chainName)}
            {@const group = CHAIN_INFO[chainName]}
            {@const chain = settings.studio[chainName]}
            {@const busy = editing !== null || reordering !== ""}
            {#if isEditingStudio(chainName, null)}
              <StudioBackupEditor chain={chainName} index={null} original={null} providers={settings.providers} {revision} onsaved={(result) => saved(result, `${group.label}: backup saved.`)} oncancel={() => (editing = null)} />
            {/if}
            {#each chain as item, index (index)}
              {#if isEditingStudio(chainName, index)}
                <StudioBackupEditor chain={chainName} {index} original={item} providers={settings.providers} {revision} onsaved={(result) => saved(result, `${group.label}: backup saved.`)} oncancel={() => (editing = null)} />
              {/if}
            {/each}
            <Card.Root aria-labelledby={`studio-${chainName}-title`}>
              <Card.Header>
                <Card.Title id={`studio-${chainName}-title`} class="text-lg">{group.label}</Card.Title>
                <Card.Description>{group.summary}</Card.Description>
              </Card.Header>
              <Card.Content class="flex flex-1 flex-col gap-3">
                {#if chain.length}
                  <ol class="grid gap-3">
                    {#each chain as item, index (index)}
                      {@const name = `${group.label} backup ${index + 1}`}
                      <li class="text-sm">
                        <div class="flex items-start gap-2">
                          <span class="font-mono text-xs text-muted-foreground">{String(index + 1).padStart(2, "0")}</span>
                          <div class="min-w-0 flex-1">
                            <p class="font-mono break-all">{item.model}</p>
                            <p class="flex flex-wrap items-center gap-x-1 text-xs text-muted-foreground">
                              {STUDIO_PROVIDERS[item.provider]?.label ?? item.provider}{item.provider === "gemini_tts" ? (item.paid ? " · paid key" : " · skipped until marked paid") : ` · ${POLICY_INFO[item.data_policy].label}`}
                              {#if item.data_policy !== "zdr"}<InfoGlyph label="Zero data retention" class="size-5">{ZDR_RECOMMENDATION} {ACCOUNT_ZDR_NOTE}</InfoGlyph>{/if}
                            </p>

                            {#if !item.key_set}<p class="text-coral">Add {item.api_key_env} to .env</p>{/if}
                          </div>
                        </div>
                        <div class="ml-6 flex flex-wrap items-center gap-1">
                          <Button variant="ghost" size="xs" onclick={() => (editing = { studio: chainName, index })} disabled={busy}>
                            <Pencil data-icon="inline-start" aria-hidden="true" /> Edit <span class="sr-only">{name}</span>
                          </Button>
                          <Button variant="ghost" size="xs" onclick={() => testStudio(chainName, index)} disabled={tests[`studio:${chainName}:${index}`] === "running"}>
                            <Activity data-icon="inline-start" aria-hidden="true" /> Test <span class="sr-only">{name}</span>
                          </Button>
                          {#if index > 0}
                            <Button variant="ghost" size="xs" onclick={() => moveStudio(chainName, index)} disabled={busy}>
                              <ArrowUp data-icon="inline-start" aria-hidden="true" /> Move up <span class="sr-only">{name}</span>
                            </Button>
                          {/if}
                          {#if index < chain.length - 1}
                            <Button variant="ghost" size="xs" onclick={() => moveStudio(chainName, index + 1)} disabled={busy}>
                              <ArrowDown data-icon="inline-start" aria-hidden="true" /> Move down <span class="sr-only">{name}</span>
                            </Button>
                          {/if}
                          <Button variant="ghost" size="xs" onclick={() => (removingBackup = { chain: chainName, index, item })} disabled={busy}>
                            <Trash data-icon="inline-start" aria-hidden="true" /> Remove <span class="sr-only">{name}</span>
                          </Button>
                        </div>
                        <div class="ml-6">{@render result(`studio:${chainName}:${index}`)}</div>
                      </li>
                    {/each}
                  </ol>
                {:else}
                  <div class="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-dashed p-3.5">
                    <p class="text-sm text-muted-foreground">No cloud backup: only the local studio.</p>
                    {#if !settings.providers.some((item) => item.type === "media")}
                      <Button variant="ghost" size="xs" onclick={() => { section = "providers"; editing = { provider: null }; }} disabled={busy}>
                        <Plus data-icon="inline-start" aria-hidden="true" /> Add provider <span class="sr-only">for {group.label.toLowerCase()}</span>
                      </Button>
                    {/if}
                  </div>
                {/if}
                {#if reordering === `studio-${chainName}`}
                  <p role="status" class="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <LoaderCircle class="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…
                  </p>
                {/if}
              </Card.Content>
              <Card.Footer class="flex-wrap gap-2">
                <Button variant="secondary" onclick={() => (editing = { studio: chainName, index: null })} disabled={busy || chain.length >= 5} aria-describedby={editing !== null ? `studio-${chainName}-wait` : undefined}>
                  <Plus data-icon="inline-start" aria-hidden="true" /> Add backup <span class="sr-only">for {group.label.toLowerCase()}</span>
                </Button>
                {@render waitNote(`studio-${chainName}-wait`)}
              </Card.Footer>
            </Card.Root>
          {/each}
        </div>
      </section>
    </Tabs.Content>

    <Tabs.Content value="providers">
      <div class="mb-5 flex flex-wrap items-center justify-between gap-3">
        <p class="flex items-center gap-1 text-sm text-muted-foreground">
          {settings.providers.length} {settings.providers.length === 1 ? "provider" : "providers"}
          <InfoGlyph label="About providers">
            Each provider is an address and a key variable. Key values live in .env; this page only shows whether each is set.
          </InfoGlyph>
        </p>
        <Button onclick={() => (editing = { provider: null })} disabled={editing !== null} aria-describedby={editing !== null ? "provider-add-wait" : undefined}>
          <Plus data-icon="inline-start" aria-hidden="true" /> Add provider
        </Button>
        {@render waitNote("provider-add-wait")}
      </div>
      {#if isEditingProvider(null)}
        <div class="mb-8 grid"><ProviderEditor original={null} {settings} {revision} onsaved={providerSaved} oncancel={() => (editing = null)} /></div>
      {/if}
      {#each TYPES as group (group)}
        {@const list = settings.providers.filter((item) => item.type === group)}
        <section aria-labelledby={`providers-${group}`} aria-describedby={`providers-${group}-summary`} class="mb-8">
          <div class="mb-3 flex flex-wrap items-end justify-between gap-2">
            <div>
              <h2 id={`providers-${group}`} class="text-lg font-semibold">{TYPE_INFO[group].heading}</h2>
              <p id={`providers-${group}-summary`} class="text-sm text-muted-foreground">{TYPE_INFO[group].summary}</p>
            </div>
            <p class="kicker">{String(list.length).padStart(2, "0")} {list.length === 1 ? "provider" : "providers"}</p>
          </div>
          {#if list.length === 0}
            <StatusNote
              title={`No ${TYPE_INFO[group].heading.toLowerCase()}`}
              detail={group === "media" ? "Add one, then pick it for a studio backup under Models." : "Add one, then pick it for a role under Models."}
            >
              <Button variant="secondary" size="sm" onclick={() => (editing = { provider: null })} disabled={editing !== null}>
                <Plus data-icon="inline-start" aria-hidden="true" /> Add provider <span class="sr-only">for {TYPE_INFO[group].heading.toLowerCase()}</span>
              </Button>
            </StatusNote>
          {/if}
          <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {#each list as provider, n (provider.name)}{@render providerCard(settings, provider, `${group}-${n}`)}{/each}
          </div>
        </section>
      {/each}
    </Tabs.Content>

    <Tabs.Content value="permissions">
      <div class="mb-5 flex flex-wrap items-center gap-2">
        <label for="permission-search" class="sr-only">Find a setting</label>
        <div class="relative min-w-0 flex-1 basis-64">
          <SearchIcon class="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <Input id="permission-search" type="search" bind:value={permissionQuery} placeholder="Find a setting" class="pl-9" />
        </div>
        <InfoGlyph label="How these settings apply">Each switch applies at once and is saved to config.toml.</InfoGlyph>
      </div>
      {#if visibleSections.length === 0}
        <StatusNote title="No setting matches" detail="Try another word, such as photos, groups or leak." />
      {/if}
      <div class="grid gap-6">
        {#each visibleSections as group (group.id)}
          <section aria-labelledby={`perm-section-${group.id}`}>
            <h2 id={`perm-section-${group.id}`} class="mb-3 text-lg font-semibold">{group.label}</h2>
            {#if group.items.length}
              <Card.Root class="py-2">
                <ul class="divide-y">
                  {#each group.items as item (item.id)}{@render permissionRow(item)}{/each}
                </ul>
                {#if group.id === "safety" && group.extras}
                  <div class="border-t"><LeakScan bind:revision onsaved={reloadPermissions} /></div>
                {/if}
              </Card.Root>
            {:else if group.id === "safety" && group.extras}
              <Card.Root class="py-2"><LeakScan bind:revision onsaved={reloadPermissions} /></Card.Root>
            {/if}
            {#if group.id === "media" && group.extras}
              <MediaLimits bind:revision />
            {/if}
            {#if group.id === "desktop" && group.extras && desktopSteps}
              <DesktopSteps bind:revision budget={desktopSteps} onsaved={(result) => (desktopSteps = result.desktop_steps)} />
            {/if}
          </section>
        {/each}
      </div>
    </Tabs.Content>

    <Tabs.Content value="services">
      <Services bind:revision bind:restartNeeded={servicesRestart} />
    </Tabs.Content>
  </Tabs.Root>
{/if}

<AlertDialog.Root open={pending !== null} onOpenChange={(open) => !open && (pending = null)}>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Turn off the leak check?</AlertDialog.Title>
      <AlertDialog.Description>
        Dom's replies and network plugin calls go out without Jev screening them for private information.
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">Keep it on</AlertDialog.Cancel>
      <AlertDialog.Action variant="destructive-solid" onclick={() => pending && setPermission(pending, false)}>Turn off</AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<AlertDialog.Root open={removingBackup !== null} onOpenChange={(open) => !open && (removingBackup = null)}>
  <AlertDialog.Content>
    {#if removingBackup}
      <AlertDialog.Header>
        <AlertDialog.Title>Remove {removingBackup.item.model}?</AlertDialog.Title>
        <AlertDialog.Description>
          It leaves the {CHAIN_INFO[removingBackup.chain].label.toLowerCase()} backups in config.toml. Its key stays in .env,
          and you can add it again later.
        </AlertDialog.Description>
      </AlertDialog.Header>
      <AlertDialog.Footer>
        <AlertDialog.Cancel variant="outline">Keep it</AlertDialog.Cancel>
        <AlertDialog.Action variant="destructive-solid" onclick={() => removingBackup && removeStudio(removingBackup.chain, removingBackup.index)}>Remove</AlertDialog.Action>
      </AlertDialog.Footer>
    {/if}
  </AlertDialog.Content>
</AlertDialog.Root>

<AlertDialog.Root open={removing !== null} onOpenChange={(open) => !open && (removing = null)}>
  <AlertDialog.Content>
    {#if removing?.uses.length}
      <AlertDialog.Header>
        <AlertDialog.Title>{removing.name} is still in use</AlertDialog.Title>
        <AlertDialog.Description>
          {removing.uses.map(useLabel).join(", ")} {removing.uses.length === 1 ? "uses" : "use"} it. Pick another provider for
          {removing.uses.length === 1 ? "that role" : "those roles"} under Models first.
        </AlertDialog.Description>
      </AlertDialog.Header>
      <AlertDialog.Footer>
        <AlertDialog.Cancel variant="outline">OK</AlertDialog.Cancel>
      </AlertDialog.Footer>
    {:else if removing}
      <AlertDialog.Header>
        <AlertDialog.Title>Remove {removing.name}?</AlertDialog.Title>
        <AlertDialog.Description>
          Its table leaves config.toml. Nothing uses it, and you can add it again later.
        </AlertDialog.Description>
      </AlertDialog.Header>
      <AlertDialog.Footer>
        <AlertDialog.Cancel variant="outline">Keep it</AlertDialog.Cancel>
        <AlertDialog.Action variant="destructive-solid" onclick={() => removing && removeProvider(removing)}>Remove</AlertDialog.Action>
      </AlertDialog.Footer>
    {/if}
  </AlertDialog.Content>
</AlertDialog.Root>
