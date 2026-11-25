<script lang="ts" module>
  import type { Status } from "./status";

  /** What one strip shows: the rest of an item lives in its details. */
  export type Strip = { status: Status; title: string; at: Date | string; meta?: string };
</script>

<script lang="ts" generics="T">
  import X from "@lucide/svelte/icons/x";
  import { Dialog } from "bits-ui";
  import { tick, type Snippet } from "svelte";
  import { MediaQuery } from "svelte/reactivity";
  import { Button } from "$lib/components/ui/button";
  import { cn } from "$lib/utils";
  import IconButton from "./IconButton.svelte";
  import StatusGlyph from "./StatusGlyph.svelte";
  import { ago, STATUS } from "./status";

  // E76: a newest-first list of one-line strips. A strip opens its details beside the list on a wide screen, or in
  // a bottom sheet on a phone. Generic over the item, so Work tasks, Turn log turns, chat messages and reminders
  // share it: `strip` says what the line shows, `details` fills the panel, `actions` adds trailing glyphs.
  let {
    items,
    key,
    strip,
    label,
    details,
    actions,
    detailTitle,
    selected = $bindable(null),
    hasMore = false,
    onmore,
    empty,
    now,
    class: className,
  }: {
    items: T[];
    key: (item: T) => string;
    strip: (item: T) => Strip;
    /** The list's accessible name, e.g. "Tasks, newest first". */
    label: string;
    details: Snippet<[T]>;
    actions?: Snippet<[T]>;
    /** The panel's heading; the strip's title when left out. */
    detailTitle?: (item: T) => string;
    selected?: string | null;
    hasMore?: boolean;
    /** Load the next page. The list watches its end and calls this as it comes into view. */
    onmore?: () => Promise<void> | void;
    empty?: Snippet;
    /** The clock relative times count from; fixtures pin it so screenshots stay still. */
    now?: Date;
    class?: string;
  } = $props();

  const uid = $props.id();
  const wide = new MediaQuery("(min-width: 1024px)");
  const current = $derived(selected === null ? undefined : items.find((item) => key(item) === selected));
  const open = $derived(current !== undefined);

  let loading = $state(false);
  let loaded = $state("");
  let sentinel = $state<HTMLElement | null>(null);
  let heading = $state<HTMLElement | null>(null);
  let sheetTitle = $state<HTMLElement | null>(null);

  function stripId(id: string) {
    return `${uid}-strip-${id}`;
  }

  async function show(item: T) {
    selected = key(item);
    // The panel's heading takes focus, so a keyboard or screen-reader user lands on what opened. The sheet moves
    // focus itself (see onOpenAutoFocus).
    if (wide.current) {
      await tick();
      heading?.focus();
    }
  }

  async function close() {
    const id = selected;
    selected = null;
    if (id === null) return;
    await tick();
    document.getElementById(stripId(id))?.focus();
  }

  async function more() {
    if (loading || !onmore) return;
    loading = true;
    const before = items.length;
    try {
      await onmore();
      await tick();
      loaded = items.length > before ? `Loaded ${items.length - before} more.` : "";
    } finally {
      loading = false;
    }
    // An observer reports only changes: if the end is still in view after a short page, keep going.
    if (hasMore && sentinel && sentinel.getBoundingClientRect().top < innerHeight + 240) more();
  }

  // Progressive loading: when the end of the list scrolls into view, fetch the next page. The button stays for
  // keyboards and for screens where the observer never fires.
  $effect(() => {
    if (!sentinel || !hasMore || !onmore || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => entries.some((entry) => entry.isIntersecting) && more(), { rootMargin: "240px" });
    observer.observe(sentinel);
    return () => observer.disconnect();
  });

  let swipe = -1;
  function swiped(event: PointerEvent) {
    if (swipe >= 0 && event.clientY - swipe > 60) close();
    swipe = -1;
  }

  function keydown(event: KeyboardEvent) {
    if (event.key === "Escape" && open && wide.current) {
      event.preventDefault();
      close();
    }
  }

  const title = $derived(current === undefined ? "" : (detailTitle?.(current) ?? strip(current).title));
</script>

{#snippet body(item: T)}
  {@const line = strip(item)}
  <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-meta text-muted-foreground">
    <StatusGlyph status={line.status} word />
    <time datetime={new Date(line.at).toISOString()} title={new Date(line.at).toLocaleString()} class="tabular-nums">{ago(line.at, now)}</time>
    {#if line.meta}<span>{line.meta}</span>{/if}
  </div>
  <div class="mt-4">{@render details(item)}</div>
{/snippet}

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class={cn("grid items-start gap-4", open && wide.current && "lg:grid-cols-[minmax(0,1fr)_minmax(20rem,24rem)]", className)}
  onkeydown={keydown}
>
  <div class="min-w-0 rounded-xl border bg-surface">
    {#if items.length === 0}
      <div class="p-4">{@render empty?.()}</div>
    {:else}
      <ul aria-label={label} class="divide-y divide-line/70">
        {#each items as item (key(item))}
          {@const id = key(item)}
          {@const line = strip(item)}
          {@const here = id === selected}
          <li
            class={cn(
              "group/strip flex min-h-11 items-center gap-1 pr-1.5 transition-colors duration-(--motion-quick) first:rounded-t-xl last:rounded-b-xl hover:bg-raised/60",
              here && "bg-tint shadow-[inset_2px_0_0_var(--signal)] hover:bg-tint",
            )}
          >
            <button
              type="button"
              id={stripId(id)}
              class="flex min-h-11 min-w-0 flex-1 items-center gap-3 rounded-lg py-2 pr-1 pl-3.5 text-left outline-offset-[-2px]"
              aria-expanded={here}
              aria-controls={here ? `${uid}-details` : undefined}
              onclick={() => (here ? close() : show(item))}
            >
              <!-- Read as "title, status, meta, time": the glyph's word follows the title for screen readers. -->
              <span aria-hidden="true" class="contents"><StatusGlyph status={line.status} /></span>
              <span class="relative min-w-0 flex-1 truncate text-body text-foreground">{line.title}<span class="sr-only">, {STATUS[line.status].label}</span></span>
              {#if line.meta}<span class="hidden max-w-[12rem] shrink-0 truncate text-meta text-muted-foreground sm:block">{line.meta}</span>{/if}
              <time datetime={new Date(line.at).toISOString()} class="shrink-0 text-meta tabular-nums text-muted-foreground">{ago(line.at, now)}</time>
            </button>
            {#if actions}<div class="flex shrink-0 items-center">{@render actions(item)}</div>{/if}
          </li>
        {/each}
      </ul>
      {#if hasMore && onmore}
        <div bind:this={sentinel} class="border-t p-2">
          <Button variant="ghost" size="sm" class="w-full" disabled={loading} onclick={more}>{loading ? "Loading…" : "Load more"}</Button>
        </div>
      {/if}
    {/if}
    <p class="sr-only" role="status">{loaded}</p>
  </div>

  {#if current !== undefined && wide.current}
    <!-- Wide screens: the details sit beside the list, which stays usable. Esc or the close glyph puts focus back
         on the strip. -->
    <section
      id={`${uid}-details`}
      aria-labelledby={`${uid}-details-title`}
      class="sticky top-4 max-h-[calc(100dvh-2rem)] overflow-y-auto rounded-xl border bg-overlay p-4 shadow-[0_18px_40px_-28px_var(--shadow)] motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-right-2 motion-safe:duration-(--motion-calm)"
    >
      <div class="flex items-start gap-2">
        <h2 id={`${uid}-details-title`} bind:this={heading} tabindex="-1" class="min-w-0 flex-1 text-title font-semibold tracking-tight break-words outline-none">{title}</h2>
        <IconButton label="Close details" icon={X} shortcut="Esc" onclick={close} class="-mt-1 -mr-1" />
      </div>
      <div class="mt-2">{@render body(current)}</div>
    </section>
  {/if}
</div>

<!-- Phones: the same details in a bottom sheet, a modal dialog that traps focus and closes on Esc, the overlay or
     a swipe down. -->
<Dialog.Root open={open && !wide.current} onOpenChange={(value) => !value && close()}>
  <Dialog.Portal>
    <Dialog.Overlay class="fixed inset-0 z-50 bg-canvas/75 data-closed:animate-out data-closed:fade-out-0 data-open:animate-in data-open:fade-in-0 motion-reduce:animate-none supports-backdrop-filter:backdrop-blur-xs" />
    <Dialog.Content
      id={`${uid}-details`}
      class="fixed inset-x-0 bottom-0 z-50 flex max-h-[85dvh] flex-col rounded-t-2xl border-t bg-overlay text-foreground shadow-xl outline-none data-closed:animate-out data-closed:slide-out-to-bottom data-open:animate-in data-open:slide-in-from-bottom duration-(--motion-calm) motion-reduce:animate-none"
      onOpenAutoFocus={(event) => {
        // Land on the title, like the side panel, rather than on the close glyph (which would pop its tooltip).
        event.preventDefault();
        sheetTitle?.focus();
      }}
      onCloseAutoFocus={(event) => event.preventDefault()}
    >
      {#if current !== undefined}
        <!-- The grab handle: a swipe down closes the sheet, like Esc and a tap on the overlay. -->
        <div class="flex touch-none justify-center pt-2.5 pb-3" aria-hidden="true" onpointerdown={(event) => (swipe = event.clientY)} onpointerup={swiped}>
          <span class="h-1 w-10 rounded-full bg-line"></span>
        </div>
        <div class="flex items-start gap-2 px-4">
          <Dialog.Title bind:ref={sheetTitle} tabindex={-1} class="min-w-0 flex-1 text-title font-semibold tracking-tight break-words outline-none">{title}</Dialog.Title>
          <IconButton label="Close details" icon={X} onclick={close} class="-mt-1 -mr-1" />
        </div>
        <Dialog.Description class="sr-only">Details of this item</Dialog.Description>
        <div class="mt-2 overflow-y-auto px-4 pb-[max(1.25rem,env(safe-area-inset-bottom))]">{@render body(current)}</div>
      {/if}
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
