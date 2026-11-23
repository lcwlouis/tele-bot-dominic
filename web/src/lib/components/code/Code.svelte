<script lang="ts">
  import { highlight, languageOf } from "$lib/highlight";
  import { cn } from "$lib/utils";

  // A block of code, highlighted when its language is known from `language` (a fence word) or `path` (an extension).
  // The markup comes from highlight.js, which escapes the text and adds only class-marked spans.
  let {
    text,
    language = null,
    path = null,
    focusable = false,
    class: className = "",
  }: { text: string; language?: string | null; path?: string | null; focusable?: boolean; class?: string } = $props();
  const html = $derived(highlight(text, languageOf(language) ?? languageOf(path)));
</script>

<!-- Focusable when it scrolls, so a keyboard can scroll it too. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<pre tabindex={focusable ? 0 : undefined} class={cn("code font-mono", className)}><code>{#if html !== null}{@html html}{:else}{text}{/if}</code></pre>
