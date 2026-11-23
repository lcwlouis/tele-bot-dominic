<script lang="ts">
  import { untrack } from "svelte";
  import { send, type ModelSettings, type ProviderKind, type ProviderSetup } from "$lib/api";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { Label } from "$lib/components/ui/label";
  import * as Select from "$lib/components/ui/select";
  import { KEY_PATTERN, KIND_INFO, KINDS, useLabel } from "./roles";

  // Adds a provider (original null) or edits one. Saving tests it first (dom/web/provider_settings.py): through the
  // first role that uses it, or by fetching its model list. Editing the address or key moves every role that uses it.
  let {
    original,
    settings,
    revision,
    onsaved,
    oncancel,
  }: {
    original: ProviderSetup | null;
    settings: ModelSettings;
    revision: string;
    onsaved: (saved: ModelSettings) => void;
    oncancel: () => void;
  } = $props();

  const start = untrack(() => original);
  let name = $state(start?.name ?? "");
  let kind = $state<ProviderKind>(start?.kind ?? "openai");
  let baseUrl = $state(start?.base_url ?? "");
  // A new provider starts with an empty key variable: the owner names one (PR #11's rule).
  let keyEnv = $state(start?.api_key_env ?? "");
  let saving = $state(false);
  let error = $state("");
  const id = "provider-editor";

  const suggestion = (value: ProviderKind) =>
    settings.search_providers.find((item) => item.id === value)?.default_base_url || KIND_INFO[value].url;

  /** A kind fills in its usual address, unless the owner typed their own. */
  function pickKind(value: ProviderKind) {
    const suggested = new Set(KINDS.map(suggestion));
    if (!baseUrl || suggested.has(baseUrl)) baseUrl = suggestion(value);
    kind = value;
    error = "";
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    saving = true;
    error = "";
    try {
      onsaved(await send<ModelSettings>("/api/providers/save", {
        revision,
        original: start?.name ?? null,
        name: name.trim(),
        kind,
        base_url: baseUrl.trim(),
        api_key_env: keyEnv.trim() || null,
      }, { quiet: true }));
    } catch (failure) {
      error = failure instanceof Error ? failure.message : "Couldn't save the provider.";
    } finally {
      saving = false;
    }
  }
</script>

<Card.Root class="col-span-full" aria-labelledby={`${id}-title`}>
  <form onsubmit={submit} class="contents">
    <Card.Header>
      <p class="kicker">{start ? "Editing" : "New provider"}</p>
      <Card.Title id={`${id}-title`} class="text-xl">{start ? start.name : "Add a provider"}</Card.Title>
      <Card.Description>
        {#if start?.uses.length}
          Used by {start.uses.map(useLabel).join(" · ")}. A new address or key variable moves them all.
        {:else}
          Pick it for a role on the Models tab once it's saved.
        {/if}
      </Card.Description>
    </Card.Header>
    <Card.Content class="grid gap-3 sm:grid-cols-2">
      <div class="grid gap-1.5">
        <Label for={`${id}-name`}>Name</Label>
        <Input id={`${id}-name`} bind:value={name} oninput={() => (error = "")} required maxlength={40} pattern={"[A-Za-z0-9][A-Za-z0-9 ._\\-]*"} autocomplete="off" />
      </div>
      <div class="grid gap-1.5">
        <Label id={`${id}-kind-label`}>Kind</Label>
        <Select.Root type="single" value={kind} onValueChange={(value) => pickKind(value as ProviderKind)}>
          <Select.Trigger class="h-9 w-full" aria-labelledby={`${id}-kind-label`}>{KIND_INFO[kind].label}</Select.Trigger>
          <Select.Content>
            {#each KINDS as value (value)}
              <Select.Item {value} label={KIND_INFO[value].label} />
            {/each}
          </Select.Content>
        </Select.Root>
      </div>
      <div class="grid gap-1.5 sm:col-span-2">
        <Label for={`${id}-url`}>Base URL</Label>
        <Input
          id={`${id}-url`}
          type="url"
          required
          bind:value={baseUrl}
          oninput={() => (error = "")}
          placeholder="https://models.example.org/v1"
          class="font-mono"
          autocomplete="off"
          spellcheck={false}
        />
      </div>
      <div class="grid gap-1.5 sm:col-span-2">
        <Label for={`${id}-key`}>Key variable</Label>
        <Input
          id={`${id}-key`}
          bind:value={keyEnv}
          oninput={() => (error = "")}
          pattern={KEY_PATTERN}
          placeholder="None"
          class="font-mono"
          autocomplete="off"
          spellcheck={false}
          aria-describedby={`${id}-key-hint`}
        />
        <p id={`${id}-key-hint`} class="text-xs text-muted-foreground">
          The name of a variable in <code class="font-mono">.env</code>, never the key itself. A variable that already
          holds a key can't be pointed at a new host from here.
        </p>
      </div>
      {#if error}
        <p role="alert" class="text-sm text-coral sm:col-span-2">{error}</p>
      {/if}
    </Card.Content>
    <Card.Footer class="flex flex-wrap justify-end gap-2">
      <Button type="button" variant="outline" onclick={oncancel} disabled={saving}>Cancel</Button>
      <Button type="submit" disabled={saving}>{saving ? "Testing and saving…" : start ? "Save provider" : "Add provider"}</Button>
    </Card.Footer>
  </form>
</Card.Root>
