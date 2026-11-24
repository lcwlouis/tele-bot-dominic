<script lang="ts">
  import ArrowUp from "@lucide/svelte/icons/arrow-up";
  import Plus from "@lucide/svelte/icons/plus";
  import X from "@lucide/svelte/icons/x";
  import { untrack } from "svelte";
  import { ApiError, send, type ModelSettings, type ProviderSetup, type SearchProviderId, type SettingsRole } from "$lib/api";
  import * as AlertDialog from "$lib/components/ui/alert-dialog";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { Label } from "$lib/components/ui/label";
  import * as Select from "$lib/components/ui/select";
  import ModelPicker from "./ModelPicker.svelte";
  import RoutePrivacy from "./RoutePrivacy.svelte";
  import { effortsFor, findModel } from "./modelLists.svelte";
  import { CLEARABLE, KEY_PATTERN, KIND_INFO, MAX_SLOTS, ROLE_INFO, family, host, isDeepSeekChat, kindsFor, slotLabel, DEEPSEEK_EFFORTS } from "./roles";

  // Edits one role's chain: its primary, then its own fallbacks in order. Each slot picks a provider (from the
  // Providers tab) and a model from that provider's list, or a custom address. Nothing is written until Save, which
  // tests every new slot on the server first (dom/web/model_settings.py update_models).
  let {
    role,
    settings,
    revision,
    onsaved,
    oncancel,
  }: {
    role: SettingsRole;
    settings: ModelSettings;
    revision: string;
    onsaved: (saved: ModelSettings) => void;
    oncancel: () => void;
  } = $props();

  const CUSTOM = "__custom__"; // provider names start with a letter or digit, so this never names one
  type Slot = {
    key: number;
    source: string; // a provider's name, or CUSTOM for an address typed here
    base_url: string;
    model: string;
    api_key_env: string;
    effort: string; // "default" leaves it to the provider
    provider: SearchProviderId;
    key_set: boolean | null; // null for a slot added here, whose key the server has not looked at yet
  };

  const search = $derived(role === "search");
  const triage = $derived(role === "triage");
  const info = $derived(ROLE_INFO[role]);
  let nextKey = 0;
  // The editor opens on the saved chain; later changes to the props (a reload) don't reset what the owner typed.
  const route = untrack(() => settings.roles[role]);
  const choices = untrack(() => settings.providers.filter((item) => kindsFor(role).includes(item.kind)));
  const searchKind = (kind: ProviderSetup["kind"]): SearchProviderId => (kind === "openai" || kind === "jev" ? "openrouter" : kind);

  function sourceOf(base_url: string, api_key_env: string | null, kind: string): string {
    const found = choices.find(
      (item) => item.base_url === base_url && item.api_key_env === api_key_env && family(item.kind) === family(kind as ProviderSetup["kind"]),
    );
    return found?.name ?? CUSTOM;
  }

  let slots = $state<Slot[]>(
    [route.primary, ...route.fallbacks].filter((item) => item !== null).map((item) => ({
      key: nextKey++,
      source: sourceOf(item.base_url, item.api_key_env, triage ? "jev" : (item.provider ?? "openai")),
      base_url: item.base_url,
      model: item.model,
      api_key_env: item.api_key_env ?? "",
      effort: item.reasoning_effort ?? "default",
      provider: item.provider ?? "openrouter",
      key_set: item.api_key_env ? item.key_set : null,
    })),
  );
  let errors = $state<Record<number, string>>({});
  let saving = $state(false);
  let confirmClear = $state(false);
  const hadPrimary = route.primary !== null;
  const maxSlots = $derived(triage ? 1 : MAX_SLOTS);

  const providerInfo = (id: SearchProviderId) => settings.search_providers.find((item) => item.id === id)!;
  const chosen = (slot: Slot) => choices.find((item) => item.name === slot.source);

  function suggestedModel(baseUrl: string): string {
    return settings.provider_presets.find((item) => item.base_url === baseUrl.replace(/\/+$/, ""))?.model ?? "";
  }

  function effortOptions(slot: Slot) {
    const model = findModel(slot.source === CUSTOM ? null : slot.source, slot.model.trim());
    const deepseek = isDeepSeekChat(slot.base_url); // Dom keeps DeepSeek's thinking off unless a level is chosen
    const listed = deepseek ? DEEPSEEK_EFFORTS : effortsFor(model);
    const values = slot.effort !== "default" && !listed.includes(slot.effort) ? [...listed, slot.effort] : listed;
    const label = (value: string) => (value === "none" ? "None (no thinking)" : value[0].toUpperCase() + value.slice(1));
    const fallback = deepseek ? "Thinking off (default)"
      : model?.default_effort ? `Provider default (${label(model.default_effort)})` : "Provider default";
    return [{ value: "default", label: fallback }, ...values.map((value) => ({ value, label: label(value) }))];
  }

  function edited() {
    errors = {};
  }

  function add() {
    const first = choices[0];
    const fresh = providerInfo("searxng");
    slots.push({
      key: nextKey++,
      source: first?.name ?? CUSTOM,
      base_url: first?.base_url ?? (search ? fresh.default_base_url : ""),
      model: first && !search && !triage ? suggestedModel(first.base_url) : "",
      api_key_env: first ? (first.api_key_env ?? "") : search ? fresh.default_api_key_env : "",
      effort: "default",
      provider: first ? searchKind(first.kind) : "searxng",
      key_set: first ? first.key_set : null,
    });
    edited();
    queueMicrotask(() => document.getElementById(`${role}-slot-${slots.at(-1)!.key}-first`)?.focus());
  }

  function pickSource(slot: Slot, name: string) {
    slot.source = name;
    const found = chosen(slot);
    if (found) {
      slot.base_url = found.base_url;
      slot.api_key_env = found.api_key_env ?? "";
      slot.key_set = found.api_key_env ? found.key_set : null;
      if (!search && !triage) {
        const model = suggestedModel(found.base_url);
        if (model) slot.model = model;
        if (isDeepSeekChat(found.base_url) && !DEEPSEEK_EFFORTS.includes(slot.effort)) slot.effort = "default";
      }
      if (search) slot.provider = searchKind(found.kind);
      if (search && !providerInfo(slot.provider).needs_model) slot.model = "";
    }
    edited();
  }

  function move(index: number) {
    [slots[index - 1], slots[index]] = [slots[index], slots[index - 1]];
    edited();
  }

  function remove(index: number) {
    slots.splice(index, 1);
    edited();
  }

  /** A new search provider fills in its own address and key variable, unless the owner typed their own. */
  function pickProvider(slot: Slot, id: SearchProviderId) {
    const urls = new Set(settings.search_providers.map((item) => item.default_base_url));
    const keys = new Set(settings.search_providers.map((item) => item.default_api_key_env));
    const next = providerInfo(id);
    if (urls.has(slot.base_url)) slot.base_url = next.default_base_url;
    if (keys.has(slot.api_key_env)) slot.api_key_env = next.default_api_key_env;
    if (!next.needs_model) slot.model = "";
    slot.provider = id;
    edited();
  }

  function body(slot: Slot) {
    const api_key_env = slot.api_key_env.trim() || null;
    if (search) {
      return {
        provider: slot.provider,
        base_url: slot.base_url.trim(),
        model: providerInfo(slot.provider).needs_model ? slot.model.trim() : "",
        api_key_env,
      };
    }
    if (triage) return { base_url: slot.base_url.trim(), model: slot.model.trim(), api_key_env };
    return {
      base_url: slot.base_url.trim(),
      model: slot.model.trim(),
      api_key_env,
      reasoning_effort: slot.effort === "default" ? null : slot.effort,
    };
  }

  function submit(event: SubmitEvent) {
    event.preventDefault();
    if (slots.length === 0 && hadPrimary) confirmClear = true;
    else save();
  }

  async function save() {
    confirmClear = false;
    saving = true;
    errors = {};
    const [primary = null, ...fallbacks] = slots.map(body);
    try {
      onsaved(await send<ModelSettings>("/api/models", { revision, role, primary, fallbacks }));
    } catch (error) {
      if (error instanceof ApiError) errors = Object.fromEntries(error.errors.map((item) => [item.index, item.error]));
    } finally {
      saving = false;
    }
  }
</script>

<Card.Root class="col-span-full" aria-labelledby={`${role}-editor-title`}>
  <form onsubmit={submit} class="contents">
    <Card.Header>
      <p class="kicker">Editing</p>
      <Card.Title id={`${role}-editor-title`} class="text-xl">{info.label}</Card.Title>
      <Card.Description>{info.note ?? info.summary}</Card.Description>
    </Card.Header>
    <Card.Content class="flex flex-col gap-4">
      {#if search}
        <ul class="grid gap-1.5 rounded-xl border bg-raised/40 p-3.5 text-sm">
          {#each settings.search_providers as item (item.id)}
            <li>
              <span class="font-medium">{item.label}:</span>
              <span class="text-muted-foreground">{item.data_retention}</span>
              {#if item.policy_url}
                <a class="text-signal-strong underline underline-offset-2" href={item.policy_url} target="_blank" rel="noopener noreferrer">{item.policy_label ?? "Data policy"}</a>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}

      {#if slots.length === 0}
        <p class="rounded-xl border border-dashed px-4 py-5 text-center text-sm text-muted-foreground">
          {role === "escalation" ? "Not set: escalation uses the drafting providers." : "No providers. Save to leave this role unset."}
        </p>
      {/if}

      <ol class="flex flex-col gap-3">
        {#each slots as slot, index (slot.key)}
          {@const id = `${role}-slot-${slot.key}`}
          {@const needsModel = !search || providerInfo(slot.provider).needs_model}
          {@const source = chosen(slot)}
          {@const efforts = effortOptions(slot)}
          <li>
            <fieldset class="rounded-xl border bg-raised/30 p-4" aria-describedby={errors[index] ? `${id}-error` : undefined}>
              <legend class="flex items-center gap-2 px-1 text-sm font-medium">
                <span class="font-mono text-xs text-muted-foreground">{String(index + 1).padStart(2, "0")}</span>
                {slotLabel(index)}
              </legend>
              <div class="grid gap-3 sm:grid-cols-2">
                <div class="grid gap-1.5 sm:col-span-2">
                  <Label id={`${id}-source-label`}>Provider</Label>
                  <Select.Root type="single" value={slot.source} onValueChange={(value) => pickSource(slot, value)}>
                    <Select.Trigger id={`${id}-first`} class="h-9 w-full" aria-labelledby={`${id}-source-label`}>
                      {source ? `${source.name} · ${KIND_INFO[source.kind].label}` : "Custom address"}
                    </Select.Trigger>
                    <Select.Content>
                      {#each choices as item (item.name)}
                        <Select.Item value={item.name} label={`${item.name} · ${KIND_INFO[item.kind].label}`} />
                      {/each}
                      <Select.Item value={CUSTOM} label="Custom address" />
                    </Select.Content>
                  </Select.Root>
                  {#if source}
                    <p class="text-xs break-all text-muted-foreground">
                      {host(source.base_url)}{source.api_key_env ? ` · ${source.api_key_env}` : " · no key"}
                      {#if source.api_key_env && !source.key_set}<Badge variant="destructive" class="ml-1">Key missing</Badge>{/if}
                    </p>
                  {/if}
                </div>
                {#if !source}
                  {#if search}
                    <div class="grid gap-1.5 sm:col-span-2">
                      <Label id={`${id}-provider-label`}>Search provider</Label>
                      <Select.Root type="single" value={slot.provider} onValueChange={(value) => pickProvider(slot, value as SearchProviderId)}>
                        <Select.Trigger class="h-9 w-full" aria-labelledby={`${id}-provider-label`}>
                          {providerInfo(slot.provider).label}
                        </Select.Trigger>
                        <Select.Content>
                          {#each settings.search_providers as item (item.id)}
                            <Select.Item value={item.id} label={item.label} />
                          {/each}
                        </Select.Content>
                      </Select.Root>
                    </div>
                  {/if}
                  <div class="grid gap-1.5 sm:col-span-2">
                    <Label for={`${id}-url`}>Base URL</Label>
                    <Input
                      id={`${id}-url`}
                      type="url"
                      required
                      bind:value={slot.base_url}
                      oninput={edited}
                      placeholder="https://models.example.org/v1"
                      class="font-mono"
                      autocomplete="off"
                      spellcheck={false}
                    />
                  </div>
                {/if}
                {#if needsModel}
                  <div class="grid gap-1.5 {source ? 'sm:col-span-2' : ''}">
                    <Label for={`${id}-model`}>Model ID</Label>
                    <ModelPicker
                      id={`${id}-model`}
                      bind:value={slot.model}
                      provider={source?.name ?? null}
                      listable={source?.listable ?? false}
                      {revision}
                      oninput={edited}
                    />
                  </div>
                {/if}
                {#if !source}
                  <div class="grid gap-1.5">
                    <Label for={`${id}-key`}>Key variable</Label>
                    <Input
                      id={`${id}-key`}
                      bind:value={slot.api_key_env}
                      oninput={edited}
                      pattern={KEY_PATTERN}
                      placeholder="None"
                      class="font-mono"
                      autocomplete="off"
                      spellcheck={false}
                      aria-describedby={`${id}-key-hint`}
                    />
                    <p id={`${id}-key-hint`} class="text-xs text-muted-foreground">
                      The name of a variable in <code class="font-mono">.env</code>, never the key itself.
                      {#if slot.key_set !== null && slot.api_key_env}
                        <Badge variant={slot.key_set ? "secondary" : "destructive"} class="ml-1">{slot.key_set ? "Key set" : "Key missing"}</Badge>
                      {/if}
                    </p>
                  </div>
                {/if}

                {#if !search && !triage}
                  <div class="grid gap-1.5">
                    <Label id={`${id}-effort-label`}>Reasoning effort</Label>
                    <Select.Root type="single" bind:value={slot.effort} onValueChange={edited}>
                      <Select.Trigger class="h-9 w-full" aria-labelledby={`${id}-effort-label`} aria-describedby={`${id}-effort-hint`}>
                        {efforts.find((item) => item.value === slot.effort)?.label}
                      </Select.Trigger>
                      <Select.Content>
                        {#each efforts as item (item.value)}
                          <Select.Item value={item.value} label={item.label} />
                        {/each}
                      </Select.Content>
                    </Select.Root>
                    <p id={`${id}-effort-hint`} class="text-xs text-muted-foreground">
                      {efforts.length === 1 ? "This model doesn't reason." : "Higher effort takes longer and costs more."}
                    </p>
                  </div>
                {:else if search}
                  <p class="self-end text-xs text-muted-foreground sm:pb-2.5">
                    {providerInfo(slot.provider).data_retention}
                  </p>
                {/if}
              </div>
              <RoutePrivacy url={slot.base_url} local={source?.is_local} class="mt-3" />
              {#if errors[index]}
                <p id={`${id}-error`} role="alert" class="mt-3 text-sm text-coral">{errors[index]}</p>
              {/if}
              {#if index > 0}
                <div class="mt-3 flex flex-wrap gap-2">
                  <Button type="button" variant="ghost" size="sm" onclick={() => move(index)} aria-label={`Move ${slotLabel(index)} up`}>
                    <ArrowUp data-icon="inline-start" aria-hidden="true" /> Move up
                  </Button>
                  <Button type="button" variant="ghost" size="sm" onclick={() => remove(index)} aria-label={`Remove ${slotLabel(index)}`}>
                    <X data-icon="inline-start" aria-hidden="true" /> Remove
                  </Button>
                </div>
              {/if}
            </fieldset>
          </li>
        {/each}
      </ol>

      {#if !triage || slots.length === 0}
        <div class="flex flex-wrap gap-2">
          <Button type="button" variant="secondary" onclick={add} disabled={slots.length >= maxSlots}>
            <Plus data-icon="inline-start" aria-hidden="true" /> {slots.length ? "Add fallback" : "Set primary"}
          </Button>
          {#if CLEARABLE.includes(role) && slots.length}
            <Button type="button" variant="ghost" onclick={() => { slots = []; edited(); }}>Clear role</Button>
          {/if}
        </div>
      {/if}
      {#if triage}
        <p class="text-xs text-muted-foreground">
          Triage uses one decisions endpoint, with no fallbacks. Until its key variable has a value in
          <code class="font-mono">.env</code>, triage and the leak check are off; once it does, press Test to turn them on.
        </p>
      {:else if slots.length >= maxSlots}
        <p class="text-xs text-muted-foreground">A role has at most {MAX_SLOTS - 1} fallbacks.</p>
      {/if}
      <p class="text-xs text-muted-foreground">
        Saving tests each new provider first. Add providers on the Providers tab. Put key values in <code class="font-mono">.env</code>, and check any new cloud model
        against your zero-data-retention policy before it sees conversations.
      </p>
    </Card.Content>
    <Card.Footer class="flex flex-wrap justify-end gap-2">
      <Button type="button" variant="outline" onclick={oncancel} disabled={saving}>Cancel</Button>
      <Button type="submit" disabled={saving}>{saving ? "Testing and saving…" : `Save ${info.label.toLowerCase()}`}</Button>
    </Card.Footer>
  </form>
</Card.Root>

<AlertDialog.Root bind:open={confirmClear}>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Clear {info.label.toLowerCase()}?</AlertDialog.Title>
      <AlertDialog.Description>
        {role === "escalation"
          ? "Escalation goes back to the drafting providers."
          : `Dom stops using every ${info.label.toLowerCase()} provider. You can set them up again here.`}
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">Cancel</AlertDialog.Cancel>
      <AlertDialog.Action variant="destructive-solid" onclick={save}>Clear and save</AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>
