<script lang="ts">
  import type { HTMLTextareaAttributes } from "svelte/elements";
  import { highlight, languageOf } from "$lib/highlight";
  import { cn } from "$lib/utils";

  // A textarea whose text is shown highlighted. The textarea stays the real control (focus, typing, selection,
  // screen readers); its own text is transparent over a highlighted copy that follows its size and scroll. Both share
  // one class list, so they wrap identically, and both reserve the scrollbar's gutter.
  let {
    value = $bindable(""),
    language = null,
    class: className = "",
    ...rest
  }: HTMLTextareaAttributes & { value?: string; language?: string | null; class?: string } = $props();
  let shadow = $state<HTMLPreElement | null>(null);
  // A trailing newline keeps the copy as tall as the textarea when the text ends in an empty line.
  const html = $derived(highlight(`${value}\n`, languageOf(language)));
  const shared = "font-mono whitespace-pre-wrap break-words [scrollbar-gutter:stable]";

  function follow(event: Event) {
    const area = event.currentTarget as HTMLTextAreaElement;
    if (shadow) [shadow.scrollTop, shadow.scrollLeft] = [area.scrollTop, area.scrollLeft];
  }
</script>

<div class="code relative">
  {#if html !== null}
    <pre bind:this={shadow} aria-hidden="true" class={cn(className, shared, "pointer-events-none absolute inset-0 m-0 h-full min-h-0 overflow-hidden")}>{@html html}</pre>
  {/if}
  <textarea
    {...rest}
    bind:value
    onscroll={follow}
    class={cn(className, shared, "relative block", html !== null && "bg-transparent text-transparent caret-foreground")}
  ></textarea>
</div>
