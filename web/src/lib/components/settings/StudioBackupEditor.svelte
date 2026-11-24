<script lang="ts">
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import { untrack } from "svelte";
  import {
    ApiError,
    send,
    type DataPolicy,
    type ModelList,
    type ModelSettings,
    type StudioBackup,
    type StudioChain,
    type StudioFallback,
    type StudioProvider,
  } from "$lib/api";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Checkbox } from "$lib/components/ui/checkbox";
  import { Input } from "$lib/components/ui/input";
  import { Label } from "$lib/components/ui/label";
  import * as Select from "$lib/components/ui/select";
  import { ACCOUNT_ZDR_NOTE, KEY_PATTERN, POLICIES, POLICY_INFO } from "./roles";
  import { CHAIN_INFO, STUDIO_PROVIDERS } from "./studio";

  // Adds a studio cloud backup to a chain (index null) or edits one. Saving tests it first without making any media
  // (dom/web/studio_settings.py): OpenRouter must accept the key and list the model for this kind of media; a direct
  // Gemini model must answer a models GET.
  let {
    chain,
    index,
    original,
    revision,
    onsaved,
    oncancel,
  }: {
    chain: StudioChain;
    index: number | null;
    original: StudioFallback | null;
    revision: string;
    onsaved: (saved: ModelSettings) => void;
    oncancel: () => void;
  } = $props();

  const info = untrack(() => CHAIN_INFO[chain]);
  const start = untrack(() => original);
  let provider = $state<StudioProvider>(start?.provider ?? info.providers[0]);
  let model = $state(start?.model ?? "");
  let keyEnv = $state(start?.api_key_env ?? STUDIO_PROVIDERS[info.providers[0]].key);
  let paid = $state(start?.paid ?? false);
  let supportsReference = $state(start?.supports_reference ?? true);
  let allowWithoutReference = $state(start?.allow_without_reference ?? false);
  // OpenRouter video keeps each video for download, so it can never be zero data retention.
  let policy = $state<DataPolicy>(start?.data_policy ?? (info.providers.includes("openrouter_video") ? "no_training" : "zdr"));
  let saving = $state(false);
  let error = $state("");
  let list = $state<ModelList | null>(null);
  let listState = $state<"idle" | "loading" | "error">("idle");
  let listError = $state("");
  const id = untrack(() => `studio-${chain}-editor`);

  const openrouter = $derived(provider !== "gemini_tts");
  const usesReference = $derived(provider === "openrouter_image" || provider === "openrouter_video");
  const policies = $derived(POLICIES.filter((value) => provider !== "openrouter_video" || value !== "zdr"));

  function pickProvider(value: StudioProvider) {
    const suggested = new Set(Object.values(STUDIO_PROVIDERS).map((item) => item.key));
    if (!keyEnv || suggested.has(keyEnv)) keyEnv = STUDIO_PROVIDERS[value].key;
    provider = value;
    list = null;
    listState = "idle";
    error = "";
  }

  async function loadList(refresh = false) {
    if (!openrouter || listState === "loading" || (list && !refresh)) return;
    listState = "loading";
    try {
      list = await send<ModelList>("/api/studio/models", { revision, provider, refresh }, { quiet: true });
      listState = "idle";
    } catch (failure) {
      listState = "error";
      listError = failure instanceof ApiError ? failure.message : "Couldn't fetch the list.";
    }
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    saving = true;
    error = "";
    const backup: StudioBackup = {
      provider,
      model: model.trim(),
      api_key_env: keyEnv.trim(),
      paid: provider === "gemini_tts" && paid,
      supports_reference: usesReference ? supportsReference : true,
      allow_without_reference: usesReference && allowWithoutReference,
      data_policy: openrouter ? policy : "zdr",
    };
    try {
      onsaved(await send<ModelSettings>("/api/studio/save", { revision, chain, index, backup }, { quiet: true }));
    } catch (failure) {
      error = failure instanceof Error ? failure.message : "Couldn't save the backup.";
    } finally {
      saving = false;
    }
  }
</script>

<Card.Root class="col-span-full" aria-labelledby={`${id}-title`}>
  <form onsubmit={submit} class="contents">
    <Card.Header>
      <p class="kicker">{start ? "Editing" : "New backup"} · {info.label}</p>
      <Card.Title id={`${id}-title`} class="text-xl">{start ? start.model : `Add a ${info.noun} backup`}</Card.Title>
      <Card.Description>Saving checks the key and the model first. It makes no media and costs nothing.</Card.Description>
    </Card.Header>
    <Card.Content class="grid gap-3 sm:grid-cols-2">
      <div class="grid gap-1.5">
        <Label id={`${id}-kind-label`}>Provider</Label>
        <Select.Root type="single" value={provider} onValueChange={(value) => pickProvider(value as StudioProvider)}>
          <Select.Trigger class="h-9 w-full" aria-labelledby={`${id}-kind-label`}>{STUDIO_PROVIDERS[provider].label}</Select.Trigger>
          <Select.Content>
            {#each info.providers as value (value)}
              <Select.Item {value} label={STUDIO_PROVIDERS[value].label} />
            {/each}
          </Select.Content>
        </Select.Root>
      </div>
      <div class="grid gap-1.5">
        <Label for={`${id}-key`}>Key variable</Label>
        <Input
          id={`${id}-key`}
          bind:value={keyEnv}
          oninput={() => (error = "")}
          required
          pattern={KEY_PATTERN}
          class="font-mono"
          autocomplete="off"
          spellcheck={false}
          aria-describedby={`${id}-key-hint`}
        />
      </div>
      <p id={`${id}-key-hint`} class="text-xs text-muted-foreground sm:col-span-2">
        The name of a variable in <code class="font-mono">.env</code>, never the key itself.
        {#if openrouter}
          Use a media-only key such as <code class="font-mono">OPENROUTER_MEDIA_API_KEY</code>: a key the model roles use
          can't serve a backup, so chat always keeps zero data retention. Save first, then add its value to
          <code class="font-mono">.env</code>.
        {/if}
      </p>
      <div class="grid gap-1.5 sm:col-span-2">
        <div class="flex items-center justify-between gap-2">
          <Label for={`${id}-model`}>Model</Label>
          {#if openrouter}
            <Button type="button" variant="ghost" size="xs" onclick={() => loadList(true)} disabled={listState === "loading"}>
              <RefreshCw data-icon="inline-start" aria-hidden="true" /> {list ? "Refresh list" : "Fetch list"}
            </Button>
          {/if}
        </div>
        <Input
          id={`${id}-model`}
          bind:value={model}
          oninput={() => (error = "")}
          onfocus={() => loadList()}
          required
          list={openrouter ? `${id}-models` : undefined}
          placeholder={STUDIO_PROVIDERS[provider].example}
          class="font-mono"
          autocomplete="off"
          spellcheck={false}
          aria-describedby={`${id}-model-hint`}
        />
        {#if list}
          <datalist id={`${id}-models`}>
            {#each list.models as item (item.id)}<option value={item.id}>{item.name}</option>{/each}
          </datalist>
        {/if}
        <p id={`${id}-model-hint`} class="text-xs text-muted-foreground" aria-live="polite">
          {#if listState === "loading"}
            Fetching OpenRouter's {info.noun} models…
          {:else if listState === "error"}
            <span class="text-coral">{listError}</span> You can type a model ID.
          {:else if list}
            {list.models.length} {info.noun} models listed. Type to filter, or enter any ID.
          {:else if openrouter}
            Pick from OpenRouter's {info.noun} models, or type an ID.
          {:else}
            A bare Gemini model ID, such as {STUDIO_PROVIDERS[provider].example}.
          {/if}
        </p>
      </div>
      {#if openrouter}
        <div class="grid gap-1.5 sm:col-span-2">
          <Label id={`${id}-policy-label`}>Data policy</Label>
          <Select.Root type="single" value={policy} onValueChange={(value) => (policy = value as DataPolicy)}>
            <Select.Trigger class="h-9 w-full" aria-labelledby={`${id}-policy-label`} aria-describedby={`${id}-policy-hint`}>
              {POLICY_INFO[policy].label}
            </Select.Trigger>
            <Select.Content>
              {#each policies as value (value)}
                <Select.Item {value} label={POLICY_INFO[value].label}>
                  <span class="grid">
                    <span>{POLICY_INFO[value].label}</span>
                    <span class="text-xs text-muted-foreground">{POLICY_INFO[value].line}</span>
                  </span>
                </Select.Item>
              {/each}
            </Select.Content>
          </Select.Root>
          <p id={`${id}-policy-hint`} class="text-xs text-muted-foreground">
            {POLICY_INFO[policy].line}
            {#if provider === "openrouter_video"}OpenRouter keeps each video for download, so no video endpoint keeps nothing.{/if}
            {#if policy !== "zdr"}{ACCOUNT_ZDR_NOTE}{/if}
          </p>
        </div>
      {/if}
      {#if provider === "gemini_tts"}
        <div class="flex items-start gap-2.5 sm:col-span-2">
          <Checkbox id={`${id}-paid`} bind:checked={paid} aria-describedby={`${id}-paid-hint`} />
          <div class="grid gap-0.5">
            <Label for={`${id}-paid`}>This key's Google project uses Paid Services</Label>
            <p id={`${id}-paid-hint`} class="text-xs text-muted-foreground">
              On an unpaid key, Google may use the audio to improve its products, with human review. Dom never runs Gemini
              without this. Its calls report tokens, not a price, so they don't count toward the daily spend cap.
            </p>
          </div>
        </div>
      {/if}
      {#if usesReference}
        <div class="flex items-start gap-2.5 sm:col-span-2">
          <Checkbox id={`${id}-reference`} bind:checked={supportsReference} aria-describedby={`${id}-reference-hint`} />
          <div class="grid gap-0.5">
            <Label for={`${id}-reference`}>The model takes a reference image</Label>
            <p id={`${id}-reference-hint`} class="text-xs text-muted-foreground">
              Dom sends the outfit's approved portrait{provider === "openrouter_video" ? " as the first frame" : ""}, so the
              {info.noun} shows him.
            </p>
          </div>
        </div>
        <div class="flex items-start gap-2.5 sm:col-span-2">
          <Checkbox id={`${id}-without`} bind:checked={allowWithoutReference} aria-describedby={`${id}-without-hint`} />
          <div class="grid gap-0.5">
            <Label for={`${id}-without`}>Allow it without a reference</Label>
            <p id={`${id}-without-hint`} class="text-xs text-muted-foreground">
              Off by default: without the portrait the {info.noun} shows a stranger's face.
            </p>
          </div>
        </div>
      {/if}
      {#if error}
        <p role="alert" class="text-sm text-coral sm:col-span-2">{error}</p>
      {/if}
    </Card.Content>
    <Card.Footer class="flex flex-wrap justify-end gap-2">
      <Button type="button" variant="outline" onclick={oncancel} disabled={saving}>Cancel</Button>
      <Button type="submit" disabled={saving}>{saving ? "Testing and saving…" : start ? "Save backup" : "Add backup"}</Button>
    </Card.Footer>
  </form>
</Card.Root>
