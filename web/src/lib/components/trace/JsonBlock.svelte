<script lang="ts">
  import ChevronDown from "@lucide/svelte/icons/chevron-down";
  import Copy from "@lucide/svelte/icons/copy";
  import Code from "$lib/components/code/Code.svelte";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import { toast } from "$lib/toast.svelte";

  // A folded block of a trace: a request, a response, a tool result or a prompt message. Its body is only built when
  // it is opened, so a page of traces with megabytes of prompts stays light; JSON is pretty-printed and highlighted
  // (the v0.8.3 highlighter), and text that is JSON inside a string (tool arguments, a model's reply) is shown parsed.
  let {
    label,
    value,
    meta = "",
    open = $bindable(false),
  }: { label: string; value: unknown; meta?: string; open?: boolean } = $props();

  const HIGHLIGHT_LIMIT = 200_000; // characters: past this, plain text keeps the page responsive

  function parsed(input: unknown): unknown {
    if (typeof input !== "string") return input;
    const trimmed = input.trim();
    if (!(trimmed.startsWith("{") || trimmed.startsWith("["))) return input;
    try {
      return JSON.parse(trimmed);
    } catch {
      return input;
    }
  }

  const shown = $derived(parsed(value));
  const isText = $derived(typeof shown === "string");
  const text = $derived(open ? (isText ? (shown as string) : JSON.stringify(shown, null, 2)) : "");

  async function copy() {
    const full = typeof shown === "string" ? shown : JSON.stringify(shown, null, 2);
    try {
      await navigator.clipboard.writeText(full);
      toast.show("ok", `Copied ${label.toLowerCase()}.`);
    } catch {
      toast.show("error", "Couldn't copy: the browser refused the clipboard.");
    }
  }
</script>

<details class="group/json min-w-0 rounded-lg border bg-surface" bind:open>
  <summary class="flex min-h-9 cursor-pointer list-none items-center gap-2 rounded-lg px-2.5 text-meta outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
    <ChevronDown class="size-3.5 shrink-0 text-muted-foreground transition-transform duration-(--motion-quick) group-open/json:rotate-180" aria-hidden="true" />
    <span class="font-medium">{label}</span>
    {#if meta}<span class="min-w-0 truncate text-muted-foreground">{meta}</span>{/if}
  </summary>
  {#if open}
    <div class="relative border-t">
      <div class="absolute top-1 right-1 z-10">
        <IconButton label={`Copy ${label.toLowerCase()}`} icon={Copy} onclick={copy} variant="secondary" />
      </div>
      {#if isText || text.length > HIGHLIGHT_LIMIT}
        <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
        <pre tabindex="0" class="max-h-[32rem] overflow-auto p-3 pr-12 font-mono text-[0.78rem] leading-relaxed break-words whitespace-pre-wrap">{text}</pre>
      {:else}
        <Code {text} language="json" focusable class="max-h-[32rem] overflow-auto p-3 pr-12 text-[0.78rem] leading-relaxed whitespace-pre-wrap break-words" />
      {/if}
    </div>
  {/if}
</details>
