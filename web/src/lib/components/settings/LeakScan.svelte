<script lang="ts">
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { onMount, untrack } from "svelte";
  import { getJSON, send, type LeakLevelId, type LeakScanView } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Button } from "$lib/components/ui/button";
  import { toast } from "$lib/toast.svelte";
  import { cn } from "$lib/utils";

  // How strict the leak scan is (E77): the Jev score that holds a message back. A choice saves at once; any level but
  // Off also turns the leak check on, so the page's switches reload after a save (`onsaved`).
  let { revision = $bindable(), onsaved }: { revision: string; onsaved?: () => void } = $props();

  let view = $state<LeakScanView | null>(null);
  let failed = $state("");
  let saving = $state<LeakLevelId | null>(null);

  async function load() {
    try {
      view = await getJSON<LeakScanView>("/api/leak-scan");
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(load);
  // Another save on the page moved config.toml: take its revision and the level as it now stands.
  $effect(() => {
    const current = revision;
    untrack(() => {
      if (view && current && current !== view.revision && saving === null) void load();
    });
  });

  async function choose(level: LeakLevelId) {
    if (!view || saving || level === view.level) return;
    saving = level;
    try {
      view = await send<LeakScanView>("/api/leak-scan", { revision: view.revision, level });
      revision = view.revision;
      toast.show("ok", `Leak scan: ${view.levels.find((item) => item.id === level)?.label.toLowerCase()}.`);
      onsaved?.();
    } catch {
      // the toast says why; the level shown stays the saved one
    } finally {
      saving = null;
    }
  }
</script>

<div class="px-5 py-4" role="group" aria-labelledby="leak-scan-label">
  <div class="flex items-center gap-1.5">
    <span id="leak-scan-label" class="font-medium">Leak scan level</span>
    {#if view}<InfoGlyph label="About the leak scan level">{view.help}</InfoGlyph>{/if}
    {#if saving}
      <span role="status" class="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground">
        <LoaderCircle class="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…
      </span>
    {/if}
  </div>
  {#if failed && !view}
    <StatusNote tone="error" title="Couldn't load the leak scan level" detail={`Dom's dashboard answered: ${failed}.`} class="mt-3">
      <Button variant="secondary" onclick={load}>Retry</Button>
    </StatusNote>
  {:else if view}
    {@const current = view.levels.find((item) => item.id === view!.level)}
    <div class="mt-3 grid grid-cols-2 gap-1 rounded-xl border bg-raised/40 p-1 sm:grid-cols-4">
      {#each view.levels as level (level.id)}
        <label
          class={cn(
            "flex cursor-pointer items-center justify-center rounded-lg px-3 py-2 text-sm transition-colors duration-(--motion-quick) has-focus-visible:ring-3 has-focus-visible:ring-ring/50",
            view.level === level.id ? "bg-surface font-medium text-signal-strong shadow-xs" : "text-muted-foreground hover:text-foreground",
          )}
        >
          <input
            type="radio"
            name="leak-scan"
            value={level.id}
            class="sr-only"
            checked={view.level === level.id}
            disabled={saving !== null}
            onchange={() => choose(level.id)}
          />
          {level.label}
        </label>
      {/each}
    </div>
    <p class="mt-2 text-sm text-muted-foreground" aria-live="polite">
      {#if current}{current.help}{:else}A custom threshold ({view.cutoff}) from an older config. Pick a level to replace it.{/if}
      {#if view.level === "off"}The local check for passwords and private notes stays on.{/if}
    </p>
    {#if !view.available && view.level !== "off"}
      <p class="mt-1 text-sm text-coral">Jev isn't set up, so nothing is scanned. Set up Triage under Models.</p>
    {/if}
  {/if}
</div>
