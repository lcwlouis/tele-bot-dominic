<script lang="ts">
  import Brain from "@lucide/svelte/icons/brain";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import { Button } from "$lib/components/ui/button";
  import { Input } from "$lib/components/ui/input";
  import { fetchedAgo, lists, loadList } from "./modelLists.svelte";

  // A model ID box that suggests from the provider's fetched list as you type. Any ID can still be typed by hand.
  let {
    id,
    value = $bindable(),
    provider,
    listable,
    revision,
    oninput,
  }: {
    id: string;
    value: string;
    provider: string | null; // a saved provider's name, or null for a custom address (typed IDs only)
    listable: boolean;
    revision: string;
    oninput: () => void;
  } = $props();

  const MAX_SHOWN = 40;
  let open = $state(false);
  let active = $state(-1);
  const entry = $derived(provider && listable ? lists[provider] : undefined);
  const matches = $derived.by(() => {
    const models = entry?.list?.models ?? [];
    const query = value.trim().toLowerCase();
    const found = query ? models.filter((model) => model.id.toLowerCase().includes(query) || model.name.toLowerCase().includes(query)) : models;
    return found.slice(0, MAX_SHOWN);
  });
  const total = $derived(entry?.list?.models.length ?? 0);

  function focus() {
    if (provider && listable) loadList(provider, revision);
    open = true;
  }

  function pick(modelId: string) {
    value = modelId;
    open = false;
    active = -1;
    oninput();
  }

  function keydown(event: KeyboardEvent) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!matches.length) return;
      event.preventDefault();
      open = true;
      const step = event.key === "ArrowDown" ? 1 : -1;
      active = (active + step + matches.length) % matches.length;
      document.getElementById(`${id}-option-${active}`)?.scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter" && open && active >= 0 && matches[active]) {
      event.preventDefault();
      pick(matches[active].id);
    } else if (event.key === "Escape" && open) {
      event.preventDefault();
      open = false;
    }
  }
</script>

<div class="relative">
  <Input
    {id}
    bind:value
    oninput={() => { open = true; active = -1; oninput(); }}
    onfocus={focus}
    onblur={() => (open = false)}
    onkeydown={keydown}
    required
    role={listable && provider ? "combobox" : undefined}
    aria-autocomplete={listable && provider ? "list" : undefined}
    aria-expanded={listable && provider ? open && matches.length > 0 : undefined}
    aria-controls={listable && provider ? `${id}-options` : undefined}
    aria-activedescendant={open && active >= 0 ? `${id}-option-${active}` : undefined}
    aria-describedby={`${id}-list-status`}
    placeholder={listable && provider ? "Search or type a model ID" : "Type a model ID"}
    class="font-mono"
    autocomplete="off"
    spellcheck={false}
  />
  {#if listable && provider && open && matches.length > 0}
    <ul
      id={`${id}-options`}
      role="listbox"
      aria-label="Models"
      class="absolute inset-x-0 top-full z-30 mt-1 max-h-64 overflow-y-auto rounded-lg border bg-surface p-1 text-foreground shadow-lg"
    >
      {#each matches as model, index (model.id)}
        <li
          id={`${id}-option-${index}`}
          role="option"
          aria-selected={index === active}
          class="flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 text-sm aria-selected:bg-raised hover:bg-raised"
          onmousedown={(event) => { event.preventDefault(); pick(model.id); }}
        >
          <span class="min-w-0 flex-1">
            <span class="block font-mono break-all">{model.id}</span>
            {#if model.name !== model.id}<span class="block text-xs text-muted-foreground">{model.name}</span>{/if}
          </span>
          {#if model.reasoning}
            <span class="mt-0.5 flex shrink-0 items-center gap-1 text-xs text-signal-strong"><Brain class="size-3.5" aria-hidden="true" /> Reasons</span>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</div>
<p id={`${id}-list-status`} class="flex min-h-6 flex-wrap items-center gap-x-2 text-xs text-muted-foreground" aria-live="polite">
  {#if !provider}
    Custom address: type the model ID.
  {:else if !listable}
    This provider doesn't list its models: type the ID.
  {:else if entry?.state === "loading"}
    Fetching models…
  {:else if entry?.state === "error"}
    <span class="text-coral">{entry.error}</span> You can still type an ID.
  {:else if entry?.list}
    {total} models · fetched {entry.list.fetched_at ? fetchedAgo(entry.list.fetched_at) : ""}
    {#if total > MAX_SHOWN && open}· type to narrow{/if}
  {/if}
  {#if provider && listable}
    <Button
      type="button"
      variant="link"
      size="xs"
      class="h-auto p-0"
      disabled={entry?.state === "loading"}
      onclick={() => loadList(provider, revision, true)}
    >
      <RefreshCw data-icon="inline-start" aria-hidden="true" /> Refresh list
    </Button>
  {/if}
</p>
