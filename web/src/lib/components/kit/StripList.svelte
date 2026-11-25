<script lang="ts" module>
  import type { Status } from "./status";

  /** What one strip shows: the rest of an item lives in its details. */
  export type Strip = { status: Status; title: string; at: Date | string; meta?: string };
  /** The filter bar's state, as `onfilter` hands it to a caller that searches on the server. */
  export type StripFilter = { text: string; statuses: Status[] };
</script>

<script lang="ts" generics="T">
  import ArrowLeft from "@lucide/svelte/icons/arrow-left";
  import ArrowUp from "@lucide/svelte/icons/arrow-up";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import Search from "@lucide/svelte/icons/search";
  import X from "@lucide/svelte/icons/x";
  import { Dialog } from "bits-ui";
  import { tick, type Snippet } from "svelte";
  import { MediaQuery } from "svelte/reactivity";
  import { Button } from "$lib/components/ui/button";
  import { cn } from "$lib/utils";
  import IconButton from "./IconButton.svelte";
  import StatusGlyph from "./StatusGlyph.svelte";
  import { ago, dayGroup, STATUS } from "./status";

  // E76: a newest-first list of one-line strips. A strip opens its details beside the list on a wide screen, or in
  // a bottom sheet on a phone. Generic over the item, so Work tasks, Turn log turns, chat messages and reminders
  // share it: `strip` says what the line shows, `details` fills the panel, `actions` adds trailing glyphs.
  // Long lists come for free: date group headers, an optional filter bar, a count, back to top, and strips the
  // browser skips laying out and painting while they are off screen (content-visibility). On a wide screen the
  // list is a pane of its own, from where it starts to the bottom of the screen, that scrolls inside; on a phone
  // the page scrolls, since a nested scroll area there traps the thumb and fights the browser's bars. A phone shows
  // only the newest few on the page, so what is below stays in reach, and "See all" opens the whole list in a
  // full-screen view of its own (with its own history entry, so the back gesture closes it).
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
    total,
    search,
    statuses,
    onfilter,
    toolbar,
    level = 3,
    contained = true,
    preview = 10,
    name,
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
    /** How many items there are in all, when the caller knows: the count reads "Showing 50 of 312". */
    total?: number;
    /** The text filter's label, e.g. "Filter tasks". Leave it out for no text filter. */
    search?: string;
    /** The status chips the filter bar offers. Leave it out for none. */
    statuses?: Status[];
    /** Search on the server: the list then shows `items` as they are and hands the filter here, debounced.
     *  Without it, the list filters what is loaded by each strip's title and meta. */
    onfilter?: (filter: StripFilter) => void;
    /** More controls for the filter bar, such as a chat picker. */
    toolbar?: Snippet;
    /** The heading level of the date groups, one below the list's own heading. */
    level?: 2 | 3 | 4 | 5;
    /** On a wide screen, keep the list in a pane that fills the screen and scrolls inside, so the page doesn't
     *  grow with it. Turn it off for a short list. */
    contained?: boolean;
    /** On a phone, how many of the newest items the page shows before "See all"; false shows the whole list on
     *  the page, for a page that is only this list. */
    preview?: number | false;
    /** The list's name in the phone's full view header, e.g. "Tasks"; the label when left out. */
    name?: string;
    empty?: Snippet;
    /** The clock relative times count from; fixtures pin it so screenshots stay still. */
    now?: Date;
    class?: string;
  } = $props();

  const uid = $props.id();
  const wide = new MediaQuery("(min-width: 1024px)");
  const reduced = new MediaQuery("(prefers-reduced-motion: reduce)");
  const current = $derived(selected === null ? undefined : items.find((item) => key(item) === selected));
  const open = $derived(current !== undefined);
  const boxed = $derived(contained && wide.current);
  // The phone's preview, and its full view.
  const limited = $derived(!wide.current && preview !== false && items.length > 0 && (items.length > preview || hasMore));
  let full = $state(false);
  let pane = $state<HTMLElement | null>(null);
  let fullPane = $state<HTMLElement | null>(null);
  let seeAll = $state<HTMLElement | null>(null);
  /** What scrolls the list: its pane on a wide screen, the full view on a phone, otherwise the page (null). */
  const scroller = $derived(boxed ? pane : full ? fullPane : null);

  // The filter bar. Typing settles for a moment before it applies, so a long list doesn't refilter per key.
  let text = $state("");
  let needle = $state("");
  let chosen = $state<Status[]>([]);
  const bar = $derived(search !== undefined || (statuses?.length ?? 0) > 0 || toolbar !== undefined);
  const filtering = $derived(needle !== "" || chosen.length > 0);
  $effect(() => {
    const value = text.trim().toLowerCase();
    const timer = setTimeout(() => (needle = value), 250);
    return () => clearTimeout(timer);
  });
  // A caller that searches on the server hears every change, after the first.
  let heard: string | undefined;
  $effect(() => {
    const filter: StripFilter = { text: needle, statuses: [...chosen] };
    const said = JSON.stringify(filter);
    if (heard !== undefined && said !== heard) onfilter?.(filter);
    heard = said;
  });
  function toggle(status: Status) {
    chosen = chosen.includes(status) ? chosen.filter((other) => other !== status) : [...chosen, status];
  }

  const shown = $derived.by(() => {
    if (onfilter || !filtering) return items;
    return items.filter((item) => {
      const line = strip(item);
      if (chosen.length && !chosen.includes(line.status)) return false;
      return !needle || line.title.toLowerCase().includes(needle) || (line.meta?.toLowerCase().includes(needle) ?? false);
    });
  });

  // Date groups, in the order the items come (newest first), each under its own heading.
  function grouped(list: T[]) {
    const out: { name: string; items: T[] }[] = [];
    for (const item of list) {
      const name = dayGroup(strip(item).at, now);
      const last = out.at(-1);
      if (last?.name === name) last.items.push(item);
      else out.push({ name, items: [item] });
    }
    return out;
  }
  const groups = $derived(grouped(shown));
  const previewGroups = $derived(limited && preview !== false ? grouped(items.slice(0, preview)) : []);
  const seeAllCount = $derived(total ?? (hasMore ? undefined : items.length));

  const count = $derived.by(() => {
    if (filtering && !onfilter) return `${shown.length} of ${items.length} loaded match`;
    if (total !== undefined) return `Showing ${items.length} of ${total}`;
    return `${items.length} loaded`;
  });

  let loading = $state(false);
  let failed = $state(false);
  let loaded = $state("");
  let sentinel = $state<HTMLElement | null>(null);
  let column = $state<HTMLElement | null>(null);
  let heading = $state<HTMLElement | null>(null);
  let sheetTitle = $state<HTMLElement | null>(null);

  /** A strip's element id: the preview's strips and the full view's are separate elements. */
  function stripId(id: string, inFull = full) {
    return `${uid}-${inFull ? "all" : "strip"}-${id}`;
  }

  // The full view on a phone. It takes a history entry, so the back gesture closes it rather than leaving the
  // page; the page underneath keeps its scroll position.
  function openFull() {
    full = true;
    history.pushState({ ...history.state, stripList: uid }, "");
  }
  async function closeFull() {
    if (!full) return;
    if (history.state?.stripList === uid) history.back();
    else full = false;
  }
  $effect(() => {
    const onpop = () => {
      if (full && history.state?.stripList !== uid) {
        full = false;
        selected = null;
        tick().then(() => seeAll?.focus({ preventScroll: true }));
      }
    };
    addEventListener("popstate", onpop);
    return () => removeEventListener("popstate", onpop);
  });
  // A phone turned wide shows the whole list in place.
  $effect(() => {
    if (!limited && full) closeFull();
  });

  /** The first strip, or the list itself when nothing shows. */
  function focusStart() {
    const first = column?.querySelector<HTMLElement>("[data-strip]") ?? column?.querySelector<HTMLElement>("[data-list-start]");
    first?.focus({ preventScroll: true });
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
    // Back to the strip; if a filter has hidden it since, to the start of the list.
    const back = document.getElementById(stripId(id));
    if (back) back.focus();
    else focusStart();
  }

  async function more() {
    if (loading || !hasMore || !onmore) return;
    loading = true;
    failed = false;
    const before = items.length;
    let grew = false;
    try {
      await onmore();
      await tick();
      grew = items.length > before;
      loaded = grew ? `Loaded ${items.length - before} more.` : "";
    } catch (error) {
      // The caller reports its own errors; here the list stops asking and offers the button again.
      console.error("Loading more failed", error);
      failed = true;
      loaded = "Couldn't load more.";
    } finally {
      loading = false;
    }
    // An observer reports only changes: if the end is still in view after a short page, keep going, but only
    // while pages bring something, so a failing or empty load can't spin.
    const end = scroller ? scroller.getBoundingClientRect().bottom : innerHeight;
    if (grew && hasMore && sentinel && sentinel.getBoundingClientRect().top < end + 240) more();
  }

  // The pane's height on a wide screen: from its top to the bottom of the screen, less the page's own bottom
  // padding, so the page fits the screen. A list that starts low on a long page (the gallery) gets a screen's
  // height less its filter bar instead, so bar and pane fill the screen once scrolled to.
  let fill = $state("calc(100dvh - 2rem)");
  $effect(() => {
    if (!boxed || !pane || !column) return;
    const [node, start] = [pane, column];
    const measure = () => {
      const box = node.getBoundingClientRect();
      const top = box.top + scrollY;
      const main = node.closest("main");
      const below = (main ? parseFloat(getComputedStyle(main).paddingBottom) : 0) + 8;
      const bar = Math.round(box.top - start.getBoundingClientRect().top);
      fill = top + below <= innerHeight / 2 ? `calc(100dvh - ${Math.round(top + below)}px)` : `calc(100dvh - 2rem - ${bar}px)`;
    };
    measure();
    // Whatever above the list changes size (a notice arriving, the filter bar wrapping) moves its top.
    const observer = new ResizeObserver(measure);
    observer.observe(document.body);
    return () => observer.disconnect();
  });

  // Progressive loading: when the end of the list scrolls into view, fetch the next page. The button stays for
  // keyboards and for screens where the observer never fires.
  $effect(() => {
    if (!sentinel || !hasMore || !onmore || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => entries.some((entry) => entry.isIntersecting) && more(), {
      root: scroller,
      rootMargin: "240px",
    });
    observer.observe(sentinel);
    return () => observer.disconnect();
  });

  // Back to top shows once the list's start is about two screens (or two panes) up and the list is still on screen.
  let far = $state(false);
  $effect(() => {
    const node = scroller ?? column;
    if (!node) return;
    const inPane = scroller !== null;
    const source: HTMLElement | Window = inPane ? node : window;
    let frame = 0;
    const measure = () => {
      frame = 0;
      if (inPane) {
        far = node.scrollTop > 2 * node.clientHeight;
        return;
      }
      const box = node.getBoundingClientRect();
      far = box.top < -2 * innerHeight && box.bottom > innerHeight / 2;
    };
    const onscroll = () => (frame ||= requestAnimationFrame(measure));
    measure();
    source.addEventListener("scroll", onscroll, { passive: true });
    addEventListener("resize", onscroll, { passive: true });
    return () => {
      cancelAnimationFrame(frame);
      source.removeEventListener("scroll", onscroll);
      removeEventListener("resize", onscroll);
    };
  });
  function top() {
    const behavior = reduced.current ? "auto" : "smooth";
    if (scroller) scroller.scrollTo({ top: 0, behavior });
    else column?.scrollIntoView({ behavior, block: "start" });
    focusStart();
  }

  let swipe = -1;
  function swiped(event: PointerEvent) {
    if (swipe >= 0 && event.clientY - swipe > 60) close();
    swipe = -1;
  }

  function keydown(event: KeyboardEvent) {
    // A control inside the list that used Esc itself (a picker closing, the filter clearing) keeps it.
    if (event.key === "Escape" && !event.defaultPrevented && open && wide.current) {
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

{#snippet row(item: T, inFull: boolean)}
  {@const itemId = key(item)}
  {@const line = strip(item)}
  {@const here = itemId === selected}
  <!-- Off-screen strips skip layout and paint (content-visibility), with their last size kept, so a long list
       scrolls smoothly while every strip stays in the page: focus, find-in-page and the open strip all survive. -->
  <li
    class={cn(
      "group/strip flex min-h-11 items-center gap-1 pr-1.5 transition-colors duration-(--motion-quick) [contain-intrinsic-size:auto_2.75rem] [content-visibility:auto] hover:bg-raised/60",
      here && "bg-tint shadow-[inset_2px_0_0_var(--signal)] hover:bg-tint",
    )}
  >
    <button
      type="button"
      id={stripId(itemId, inFull)}
      data-strip
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
{/snippet}

{#snippet days(list: { name: string; items: T[] }[], inFull: boolean, sticky: boolean)}
  {#each list as group, index (group.name)}
    {@const id = `${uid}-${inFull ? "all" : "day"}-${index}`}
    <!-- The day sticks to the top (of the pane, the full view under its header, or the screen) while its strips
         scroll under it. -->
    <svelte:element
      this={`h${level}`}
      {id}
      class={cn(
        "z-10 border-y border-line/70 bg-surface/90 px-3.5 py-1.5 font-mono text-[0.68rem] tracking-[0.14em] uppercase text-muted-foreground backdrop-blur-sm",
        sticky && (inFull ? "sticky top-14" : "sticky top-0"),
        index === 0 && !sticky && "rounded-t-xl border-t-0",
      )}
    >
      {group.name}
    </svelte:element>
    <ul aria-labelledby={id} class="divide-y divide-line/70">
      {#each group.items as item (key(item))}{@render row(item, inFull)}{/each}
    </ul>
  {/each}
{/snippet}

<!-- The whole list: filter bar, count, the days, loading and back to top. On a wide screen it sits in the page or
     its pane; on a phone in the page (when it opts out of the preview) or in the full view. -->
{#snippet whole(inFull: boolean)}
  <div bind:this={column} class="grid min-w-0 scroll-mt-4 gap-2">
    {#if bar}
      <div class="flex flex-wrap items-center gap-2">
        {#if search !== undefined}
          <div class="relative min-w-0 flex-1 basis-56">
            <Search class="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
            <label for={`${uid}-search`} class="sr-only">{search}</label>
            <input
              id={`${uid}-search`}
              type="search"
              bind:value={text}
              placeholder={search}
              autocomplete="off"
              class="h-9 w-full rounded-[0.65rem] border border-input bg-surface pr-3 pl-9 text-sm text-foreground outline-none transition-colors duration-(--motion-quick) placeholder:text-muted-foreground hover:border-signal/50 focus-visible:border-signal/70 focus-visible:ring-3 focus-visible:ring-ring/50"
              onkeydown={(event) => {
                // Esc empties the filter first; the next Esc closes the details.
                if (event.key === "Escape" && text) {
                  event.preventDefault();
                  text = "";
                }
              }}
            />
          </div>
        {/if}
        {#if statuses?.length}
          <div role="group" aria-label="Status" class="flex flex-wrap items-center gap-1.5">
            {#each statuses as status (status)}
              {@const on = chosen.includes(status)}
              <button
                type="button"
                aria-pressed={on}
                class={cn(
                  "inline-flex h-8 items-center rounded-full border px-2.5 transition-colors duration-(--motion-quick) hover:border-signal/50",
                  on ? "border-signal/60 bg-tint text-foreground" : "border-input bg-surface",
                )}
                onclick={() => toggle(status)}
              >
                <StatusGlyph {status} word />
              </button>
            {/each}
          </div>
        {/if}
        {@render toolbar?.()}
      </div>
    {/if}

    <div
      bind:this={pane}
      data-pane
      class={cn("relative min-w-0 rounded-xl border bg-surface", boxed && "overflow-y-auto")}
      style:height={boxed ? fill : undefined}
    >
      {#if items.length > 0}<p class="px-3.5 pt-2.5 pb-1.5 text-caption text-muted-foreground tabular-nums" data-count>{count}</p>{/if}
      {#if items.length === 0}
        <div class="p-4">{@render empty?.()}</div>
      {:else if shown.length === 0}
        <p class="p-4 text-body text-muted-foreground" tabindex="-1" data-list-start>Nothing loaded matches this filter.</p>
      {:else}
        <div
          role="group"
          aria-label={label}
          tabindex="-1"
          class={cn("outline-none", !(hasMore && onmore) && "[&>ul:last-child>li:last-child]:rounded-b-xl")}
          data-list-start
        >
          {@render days(groups, inFull, true)}
        </div>
      {/if}
      {#if items.length > 0 && hasMore && onmore}
        <div bind:this={sentinel} class="border-t p-2">
          <Button variant="ghost" size="sm" class="w-full" disabled={loading} onclick={more}>
            {loading ? "Loading…" : failed ? "Couldn't load more. Try again" : "Load more"}
          </Button>
        </div>
      {/if}
      {#if far}
        <!-- Floats at the bottom of the list (or its pane), clear of the details panel, while the list is on screen:
             at the right on a phone, under the thumb; centred on a wide screen, where the middle of a strip is empty. -->
        <div class="sticky bottom-4 z-20 flex h-0 items-end justify-end pr-3 lg:justify-center lg:pr-0">
          <IconButton
            label="Back to the top of the list"
            icon={ArrowUp}
            variant="outline"
            size="icon"
            side="left"
            class="rounded-full bg-overlay shadow-[0_10px_30px_-12px_var(--shadow)] motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-(--motion-calm)"
            onclick={top}
          />
        </div>
      {/if}
      <p class="sr-only" role="status">{loaded}</p>
    </div>
  </div>
{/snippet}

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class={cn("grid items-start gap-4", open && wide.current && "lg:grid-cols-[minmax(0,1fr)_minmax(20rem,24rem)]", className)}
  onkeydown={keydown}
>
  {#if limited}
    <!-- Phone: the newest few, then the way to the rest. -->
    <div class="min-w-0 rounded-xl border bg-surface" data-preview>
      <div role="group" aria-label={label}>{@render days(previewGroups, false, false)}</div>
      <button
        bind:this={seeAll}
        type="button"
        aria-haspopup="dialog"
        class="flex min-h-11 w-full items-center justify-between rounded-b-xl border-t border-line/70 px-3.5 text-body font-medium text-signal-strong outline-offset-[-2px] hover:bg-raised/60"
        onclick={openFull}
      >
        {seeAllCount === undefined ? "See all" : `See all (${seeAllCount})`}
        <ChevronRight class="size-4" aria-hidden="true" />
      </button>
    </div>
  {:else}
    {@render whole(false)}
  {/if}

  {#if current !== undefined && wide.current}
    <!-- Wide screens: the details sit beside the list, which stays usable. Beside a pane they fill its height and
         scroll on their own (absolutely placed, so long details never stretch the row); beside a page-length list
         they stick to the top. Esc or the close glyph puts focus back on the strip. -->
    <section
      id={`${uid}-details`}
      aria-labelledby={`${uid}-details-title`}
      class={cn(
        "rounded-xl border bg-overlay shadow-[0_18px_40px_-28px_var(--shadow)] motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-right-2 motion-safe:duration-(--motion-calm)",
        boxed ? "relative self-stretch" : "sticky top-4 max-h-[calc(100dvh-2rem)] overflow-y-auto p-4",
      )}
    >
      <div class={cn(boxed && "absolute inset-0 overflow-y-auto p-4")}>
        <div class="flex items-start gap-2">
          <h2 id={`${uid}-details-title`} bind:this={heading} tabindex="-1" class="min-w-0 flex-1 text-title font-semibold tracking-tight break-words outline-none">{title}</h2>
          <IconButton label="Close details" icon={X} shortcut="Esc" onclick={close} class="-mt-1 -mr-1" />
        </div>
        <div class="mt-2">{@render body(current)}</div>
      </div>
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

<!-- Phones: the whole list as its own screen, from "See all". The page scrolls here as it would on a page of its
     own; Back, Esc or the back gesture returns to the page where it was. Its strips open the same bottom sheet. -->
<Dialog.Root open={full} onOpenChange={(value) => !value && closeFull()}>
  <Dialog.Portal>
    <Dialog.Content
      bind:ref={fullPane}
      class="fixed inset-0 z-40 overflow-y-auto bg-canvas text-foreground outline-none data-closed:animate-out data-closed:fade-out-0 data-open:animate-in data-open:fade-in-0 data-open:slide-in-from-right-4 duration-(--motion-calm) motion-reduce:animate-none"
      onOpenAutoFocus={(event) => {
        // Land on the title, like the sheet, rather than on Back (which would pop its tooltip over the title).
        event.preventDefault();
        fullPane?.querySelector<HTMLElement>("[data-full-title]")?.focus();
      }}
      onCloseAutoFocus={(event) => {
        event.preventDefault();
        seeAll?.focus({ preventScroll: true });
      }}
    >
      <div class="sticky top-0 z-30 flex h-14 items-center gap-2 border-b bg-canvas/90 px-2 backdrop-blur-sm">
        <IconButton label="Back" icon={ArrowLeft} size="icon" side="bottom" onclick={closeFull} />
        <Dialog.Title tabindex={-1} data-full-title class="min-w-0 flex-1 truncate text-title font-semibold tracking-tight outline-none">{name ?? label}</Dialog.Title>
      </div>
      <Dialog.Description class="sr-only">The whole list, newest first.</Dialog.Description>
      <div class="px-4 pt-3 pb-[max(1.5rem,env(safe-area-inset-bottom))]">
        {#if full}{@render whole(true)}{/if}
      </div>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
