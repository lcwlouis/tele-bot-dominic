<script lang="ts" module>
  export type PickerOption = { value: string; label: string; detail?: string };
</script>

<script lang="ts">
  import Check from "@lucide/svelte/icons/check";
  import ChevronsUpDown from "@lucide/svelte/icons/chevrons-up-down";
  import { Combobox } from "bits-ui";
  import { cn } from "$lib/utils";

  // The app's own picker, in place of a native <select> (first for the Turn log's chat filter). Type to narrow
  // the list; arrows, Enter and Esc work as in any combobox. The label is required and visible.
  let {
    label,
    options,
    value = $bindable(""),
    placeholder = "Type to filter",
    onchange,
    class: className,
  }: {
    label: string;
    options: PickerOption[];
    value?: string;
    placeholder?: string;
    onchange?: (value: string) => void;
    class?: string;
  } = $props();

  const uid = $props.id();
  let open = $state(false);
  let query = $state("");
  const chosen = $derived(options.find((option) => option.value === value));
  const shown = $derived.by(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return options;
    return options.filter((option) => option.label.toLowerCase().includes(needle) || option.detail?.toLowerCase().includes(needle));
  });

  // Closed, the field reads as the chosen option; open, it is the filter.
  $effect(() => {
    if (!open) query = "";
  });
</script>

<div class={cn("grid gap-1.5", className)}>
  <label for={`${uid}-input`} class="text-sm leading-none font-medium select-none">{label}</label>
  <Combobox.Root
    type="single"
    bind:open
    {value}
    items={options}
    inputValue={chosen?.label ?? ""}
    onValueChange={(next) => {
      value = next;
      onchange?.(next);
    }}
  >
    <div
      class="flex h-9 w-full min-w-56 items-center rounded-[0.65rem] border border-input bg-surface pr-1 transition-colors duration-(--motion-quick) focus-within:border-signal/70 hover:border-signal/50"
    >
      <Combobox.Input
        id={`${uid}-input`}
        class="h-full min-w-0 flex-1 bg-transparent px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground"
        {placeholder}
        onclick={(event) => {
          // A click on the field opens the list, with the current choice selected so typing replaces it.
          if (open) return;
          open = true;
          event.currentTarget.select();
        }}
        oninput={(event) => {
          query = event.currentTarget.value;
          open = true;
        }}
      />
      <Combobox.Trigger
        class="grid size-7 place-items-center rounded-md text-muted-foreground hover:bg-raised hover:text-foreground"
        aria-label={`Show ${label.toLowerCase()} options`}
      >
        <ChevronsUpDown class="size-4" aria-hidden="true" />
      </Combobox.Trigger>
    </div>
    <Combobox.Portal>
      <Combobox.Content
        sideOffset={4}
        class="z-50 max-h-[min(20rem,var(--bits-combobox-content-available-height))] w-(--bits-combobox-anchor-width) min-w-56 overflow-y-auto rounded-lg bg-popover p-1 text-popover-foreground shadow-md ring-1 ring-foreground/10 data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0 motion-reduce:animate-none"
      >
        {#each shown as option (option.value)}
          <Combobox.Item
            value={option.value}
            label={option.label}
            class="flex cursor-default items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none select-none data-highlighted:bg-tint data-highlighted:text-foreground"
          >
            {#snippet children({ selected })}
              <span class="flex min-w-0 flex-1 flex-col">
                <span class="truncate">{option.label}</span>
                {#if option.detail}<span class="truncate text-caption text-muted-foreground">{option.detail}</span>{/if}
              </span>
              <Check class={cn("size-4 shrink-0 text-signal", !selected && "invisible")} aria-hidden="true" />
            {/snippet}
          </Combobox.Item>
        {:else}
          <p class="px-2 py-1.5 text-sm text-muted-foreground">Nothing matches "{query}".</p>
        {/each}
      </Combobox.Content>
    </Combobox.Portal>
  </Combobox.Root>
</div>
