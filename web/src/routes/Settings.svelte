<script lang="ts">
  import Activity from "@lucide/svelte/icons/activity";
  import Cloud from "@lucide/svelte/icons/cloud";
  import Lock from "@lucide/svelte/icons/lock";
  import Pencil from "@lucide/svelte/icons/pencil";
  import RotateCw from "@lucide/svelte/icons/rotate-cw";
  import Server from "@lucide/svelte/icons/server";
  import { onMount } from "svelte";
  import {
    getJSON,
    send,
    type ModelSettings,
    type Permission,
    type Permissions,
    type ProviderView,
    type Role,
    type TestResult,
  } from "$lib/api";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import RoleEditor from "$lib/components/settings/RoleEditor.svelte";
  import { ROLE_INFO, ROLES, host, seconds, slotLabel } from "$lib/components/settings/roles";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import * as AlertDialog from "$lib/components/ui/alert-dialog";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Switch } from "$lib/components/ui/switch";
  import * as Tabs from "$lib/components/ui/tabs";
  import type { Destination, Tab } from "$lib/nav";
  import { toast } from "$lib/toast.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  let models = $state<ModelSettings | null>(null);
  let permissions = $state<Permission[]>([]);
  // One revision for the whole page: both GETs hash the same config.toml, and every save moves it.
  let revision = $state("");
  let failed = $state("");
  let section = $state("models");
  let editing = $state<Role | null>(null);
  let tests = $state<Record<string, TestResult | "running">>({});
  let pending = $state<Permission | null>(null); // a switch waiting on its confirm dialog
  // One permission save at a time: each takes the revision the last one returned.
  let savingPermission = $state(false);

  async function load() {
    try {
      const [loadedModels, loadedPermissions] = await Promise.all([
        getJSON<ModelSettings>("/api/models"),
        getJSON<Permissions>("/api/permissions"),
      ]);
      models = loadedModels;
      permissions = loadedPermissions.items;
      revision = loadedModels.revision;
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(load);

  function saved(result: ModelSettings) {
    models = result;
    revision = result.revision;
    editing = null;
    tests = {};
    toast.show("ok", result.restart_required ? "Saved. Restart Dom to apply." : "Saved.");
  }

  /** Test a saved slot (index), or the whole saved search chain (null). */
  async function test(role: Role, index: number | null) {
    const key = `${role}:${index}`;
    tests[key] = "running";
    try {
      tests[key] = await send<TestResult>("/api/models/test", { revision, role, index });
    } catch {
      delete tests[key];
    }
  }

  // Optimistic: a switch has no content to reject, so it flips at once and flips back if the save fails.
  async function setPermission(item: Permission, enabled: boolean) {
    pending = null;
    item.enabled = enabled;
    savingPermission = true;
    try {
      const result = await send<Permissions>("/api/permissions", { revision, id: item.id, enabled });
      permissions = result.items;
      revision = result.revision;
      toast.show("ok", `${item.label}: ${enabled ? "on" : "off"}.`);
    } catch {
      item.enabled = !enabled;
    } finally {
      savingPermission = false;
    }
  }

  function flip(item: Permission, enabled: boolean) {
    if (savingPermission) return;
    // Turning the leak check off lets Dom send without the privacy screen: ask first.
    if (item.id === "leak_check" && !enabled) pending = item;
    else setPermission(item, enabled);
  }

  type Usage = { role: Role | "triage"; index: number };
  type KeyState = "none" | "set" | "missing";
  type Provider = { id: string; name: string; endpoint: ProviderView | null; base_url: string; kind: "local" | "cloud"; key: string | null; keyState: KeyState; uses: Usage[] };
  const keyState = (variable: string | null, set: boolean): KeyState => (!variable ? "none" : set ? "set" : "missing");
  const RANK: Record<KeyState, number> = { none: 0, set: 1, missing: 2 };
  /** A group of slots shows its worst key: one missing key makes the provider's key missing. */
  function worse(entry: Provider, key: string | null, state: KeyState) {
    if (RANK[state] > RANK[entry.keyState]) [entry.key, entry.keyState] = [key, state];
  }

  /** Every saved endpoint, grouped by address (and search provider): who serves Dom, and which roles use it. */
  function providers(settings: ModelSettings, searchOnly: boolean): Provider[] {
    const found = new Map<string, Provider>();
    const note = (endpoint: ProviderView, use: Usage) => {
      const id = `${endpoint.provider ?? ""} ${endpoint.base_url}`;
      const entry = found.get(id) ?? {
        id,
        name: endpoint.provider ? settings.search_providers.find((item) => item.id === endpoint.provider)!.label : host(endpoint.base_url),
        endpoint,
        base_url: endpoint.base_url,
        kind: endpoint.kind,
        key: endpoint.api_key_env,
        keyState: keyState(endpoint.api_key_env, endpoint.key_set),
        uses: [],
      };
      entry.uses.push(use);
      worse(entry, endpoint.api_key_env, keyState(endpoint.api_key_env, endpoint.key_set));
      found.set(id, entry);
    };
    for (const role of ROLES) {
      if ((role === "search") !== searchOnly) continue;
      const route = settings.roles[role];
      [route.primary, ...route.fallbacks].forEach((endpoint, index) => endpoint && note(endpoint, { role, index }));
    }
    if (!searchOnly && settings.triage.base_url) {
      const id = ` ${settings.triage.base_url}`;
      const entry = found.get(id);
      const triageKey = keyState(settings.triage.api_key_env, settings.triage.key_set);
      if (entry) {
        entry.uses.push({ role: "triage", index: 0 });
        worse(entry, settings.triage.api_key_env, triageKey);
      } else {
        found.set(id, {
          id, name: host(settings.triage.base_url), endpoint: null, base_url: settings.triage.base_url,
          kind: /^http:/.test(settings.triage.base_url) ? "local" : "cloud", key: settings.triage.api_key_env, keyState: triageKey,
          uses: [{ role: "triage", index: 0 }],
        });
      }
    }
    return [...found.values()];
  }

  const useLabel = (use: Usage) =>
    use.role === "triage" ? "Triage" : use.index === 0 ? ROLE_INFO[use.role].label : `${ROLE_INFO[use.role].label} ${slotLabel(use.index).toLowerCase()}`;
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

{#snippet slotTest(role: Role, index: number, name: string)}
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
  {#if settings.restart_required}
    <StatusNote tone="error" title="Restart Dom to apply the saved routes" detail="The running model routes differ from config.toml." class="mb-5" />
  {/if}

  <Tabs.Root bind:value={section}>
    <Tabs.List variant="line" class="no-scrollbar mb-5 h-auto! w-full justify-start gap-5 overflow-x-auto border-b pb-1">
      <Tabs.Trigger value="models" class="flex-none px-0 py-2 data-active:text-signal-strong">Models</Tabs.Trigger>
      <Tabs.Trigger value="providers" class="flex-none px-0 py-2 data-active:text-signal-strong">Providers</Tabs.Trigger>
      <Tabs.Trigger value="permissions" class="flex-none px-0 py-2 data-active:text-signal-strong">What Dom may do</Tabs.Trigger>
    </Tabs.List>

    <Tabs.Content value="models">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
        <p class="kicker">{String(ROLES.length + 1).padStart(2, "0")} jobs / routing</p>
        <Badge variant="outline"><Lock aria-hidden="true" /> Triage locked</Badge>
      </div>
      <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {#each ROLES as role (role)}
          {@const info = ROLE_INFO[role]}
          {@const route = settings.roles[role]}
          {#if editing === role}
            <RoleEditor {role} {settings} {revision} onsaved={saved} oncancel={() => (editing = null)} />
          {:else}
            {@const inherited = role === "escalation" && !route.primary}
            {@const testKey = `${role}:${role === "search" ? null : 0}`}
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
                      <p class="min-w-0 font-mono text-sm break-all text-signal-strong">{primary.model || settings.search_providers.find((item) => item.id === primary.provider)?.label}</p>
                      <Badge variant={primary.kind === "local" ? "default" : "outline"}>{primary.kind === "local" ? "Local" : "Cloud"}</Badge>
                    </div>
                    <p class="mt-1 text-sm break-all text-muted-foreground">{host(primary.base_url)}</p>
                    {#if primary.api_key_env && !primary.key_set}
                      <p class="mt-2 text-sm text-coral">Add {primary.api_key_env} to .env</p>
                    {/if}
                    {#if role === "search"}
                      <!-- The footer tests the whole chain; this tests the primary alone. -->
                      <div class="mt-2">{@render slotTest(role, 0, `${info.label} primary`)}</div>
                      {@render result(`${role}:0`)}
                    {/if}
                  </div>
                  <div>
                    <p class="kicker">If it fails</p>
                    {#if route.fallbacks.length}
                      <ol class="mt-1.5 grid gap-1">
                        {#each route.fallbacks as fallback, index (index)}
                          <li class="text-sm">
                            <div class="flex items-start gap-2">
                              <span class="font-mono text-xs text-muted-foreground">{String(index + 1).padStart(2, "0")}</span>
                              <span class="min-w-0 flex-1 font-mono break-all">{fallback.model || settings.search_providers.find((item) => item.id === fallback.provider)?.label} · {host(fallback.base_url)}</span>
                              {@render slotTest(role, index + 1, `${info.label} fallback ${index + 1}`)}
                            </div>
                            {#if fallback.api_key_env && !fallback.key_set}<p class="ml-6 text-coral">Add {fallback.api_key_env} to .env</p>{/if}
                            <div class="ml-6">{@render result(`${role}:${index + 1}`)}</div>
                          </li>
                        {/each}
                      </ol>
                    {:else}
                      <p class="mt-1.5 text-sm text-muted-foreground">
                        {role === "chat" ? "The fallback role, if set" : role === "drafting" ? "The chat route" : "No backup"}
                      </p>
                    {/if}
                  </div>
                {:else}
                  <p class="rounded-xl border border-dashed p-3.5 text-sm text-muted-foreground">
                    {inherited ? "Not set: uses the drafting providers." : "Not set."}
                  </p>
                {/if}
                {#if info.note}<p class="text-xs text-muted-foreground">{info.note}</p>{/if}
                {@render result(testKey)}
              </Card.Content>
              <Card.Footer class="gap-2">
                <Button variant="secondary" onclick={() => (editing = role)} disabled={editing !== null}>
                  <Pencil data-icon="inline-start" aria-hidden="true" /> {route.primary ? "Edit" : "Set up"}
                  <span class="sr-only">{info.label}</span>
                </Button>
                {#if route.primary || inherited}
                  <Button variant="ghost" onclick={() => test(role, role === "search" ? null : 0)} disabled={tests[testKey] === "running"}>
                    <Activity data-icon="inline-start" aria-hidden="true" /> {role === "search" ? "Test chain" : "Test"}
                    <span class="sr-only">{info.label}</span>
                  </Button>
                {/if}
              </Card.Footer>
            </Card.Root>
          {/if}
        {/each}

        <Card.Root aria-labelledby="triage-title">
          <Card.Header>
            <Card.Title id="triage-title" class="text-lg">Triage</Card.Title>
            <Card.Description>Decides whether to reply.</Card.Description>
            <Card.Action>
              <span class="grid size-9 place-items-center rounded-lg border bg-raised text-signal" aria-hidden="true"><Lock class="size-4" /></span>
            </Card.Action>
          </Card.Header>
          <Card.Content class="flex flex-1 flex-col gap-4">
            <div class="rounded-xl border bg-raised/40 p-3.5">
              <div class="flex items-start justify-between gap-2">
                <p class="min-w-0 font-mono text-sm break-all text-signal-strong">{settings.triage.model}</p>
                {@render keyBadge(keyState(settings.triage.api_key_env, settings.triage.key_set))}
              </div>
              <p class="mt-1 text-sm break-all text-muted-foreground">{host(settings.triage.base_url)}</p>
              {#if settings.triage.api_key_env && !settings.triage.key_set}
                <p class="mt-2 text-sm text-coral">Add {settings.triage.api_key_env} to .env</p>
              {/if}
            </div>
            <p class="text-xs text-muted-foreground">
              Jev, a fixed classifier: it cannot be swapped for a chat model here. Change it in config.toml.
            </p>
          </Card.Content>
        </Card.Root>
      </div>
    </Tabs.Content>

    <Tabs.Content value="providers">
      {#each [false, true] as searchOnly (searchOnly)}
        {@const list = providers(settings, searchOnly)}
        <section aria-labelledby={`providers-${searchOnly}`} class="mb-8">
          <div class="mb-3 flex flex-wrap items-end justify-between gap-2">
            <h2 id={`providers-${searchOnly}`} class="text-lg font-semibold">{searchOnly ? "Web search" : "Model providers"}</h2>
            <p class="kicker">{String(list.length).padStart(2, "0")} {list.length === 1 ? "endpoint" : "endpoints"}</p>
          </div>
          {#if list.length === 0}
            <StatusNote title={searchOnly ? "No search providers" : "No model providers"} detail="Set one up from a role under Models.">
              <Button variant="secondary" size="sm" onclick={() => (section = "models")}>Go to Models</Button>
            </StatusNote>
          {/if}
          <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {#each list as provider, n (provider.id)}
              {@const testable = provider.uses.find((use) => use.role !== "triage") as { role: Role; index: number } | undefined}
              {@const testKey = testable ? `${testable.role}:${testable.index}` : ""}
              <Card.Root aria-labelledby={`provider-${searchOnly}-${n}`}>
                <Card.Header>
                  <div class="flex items-center gap-3">
                    <span class="grid size-9 shrink-0 place-items-center rounded-lg border bg-raised text-signal" aria-hidden="true">
                      {#if provider.kind === "local"}<Server class="size-4" />{:else}<Cloud class="size-4" />{/if}
                    </span>
                    <div class="min-w-0">
                      <Card.Title id={`provider-${searchOnly}-${n}`} class="text-base break-all">{provider.name}</Card.Title>
                      <Badge variant={provider.kind === "local" ? "default" : "outline"} class="mt-1">{provider.kind === "local" ? "Local" : "Cloud"}</Badge>
                    </div>
                  </div>
                  <Card.Action>{@render keyBadge(provider.keyState)}</Card.Action>
                </Card.Header>
                <Card.Content class="flex flex-1 flex-col gap-3">
                  <p class="rounded-lg bg-raised/60 px-3 py-2 font-mono text-xs break-all text-muted-foreground">{provider.base_url}</p>
                  {#if provider.keyState === "missing"}<p class="text-sm text-coral">Add {provider.key} to .env</p>{/if}
                  {#if provider.endpoint?.data_retention}
                    <p class="text-xs text-muted-foreground">
                      {provider.endpoint.data_retention}
                      {#if provider.endpoint.policy_url}
                        <a class="text-signal-strong underline underline-offset-2" href={provider.endpoint.policy_url} target="_blank" rel="noopener noreferrer">{provider.endpoint.policy_label ?? "Data policy"}</a>
                      {/if}
                    </p>
                  {/if}
                  <div>
                    <p class="kicker">Used by</p>
                    <p class="mt-1 text-sm">{provider.uses.map(useLabel).join(" · ")}</p>
                  </div>
                  {#if testKey}{@render result(testKey)}{/if}
                </Card.Content>
                {#if testable}
                  <Card.Footer>
                    <Button variant="secondary" onclick={() => test(testable.role, testable.index)} disabled={tests[testKey] === "running"} class="ml-auto">
                      <Activity data-icon="inline-start" aria-hidden="true" /> Test <span class="sr-only">{provider.name}</span>
                    </Button>
                  </Card.Footer>
                {/if}
              </Card.Root>
            {/each}
          </div>
        </section>
      {/each}
      <p class="text-sm text-muted-foreground">
        To add, change or reorder a provider, edit a role under
        <Button variant="link" class="h-auto p-0" onclick={() => (section = "models")}>Models</Button>.
        Key values live in <code class="font-mono">.env</code>; this page only shows whether each is set.
      </p>
    </Tabs.Content>

    <Tabs.Content value="permissions">
      <Card.Root class="py-2">
        <ul class="divide-y">
          {#each permissions as item (item.id)}
            <li class="flex items-center justify-between gap-4 px-5 py-4">
              <div class="min-w-0">
                <label for={`perm-${item.id}`} class="font-medium">{item.label}</label>
                <p id={`perm-${item.id}-hint`} class="mt-0.5 text-sm text-muted-foreground">{item.description}</p>
              </div>
              <Switch
                id={`perm-${item.id}`}
                aria-describedby={`perm-${item.id}-hint`}
                bind:checked={() => item.enabled, (value) => flip(item, value)}
                disabled={savingPermission}
              />
            </li>
          {/each}
        </ul>
      </Card.Root>
      <p class="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
        <RotateCw class="size-3.5" aria-hidden="true" /> Each switch applies at once and is saved to config.toml.
      </p>
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
