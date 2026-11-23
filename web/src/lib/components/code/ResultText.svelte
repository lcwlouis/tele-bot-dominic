<script lang="ts">
  import { cn } from "$lib/utils";
  import Code from "./Code.svelte";

  // Plain text, such as a desktop task's result, whose fenced code blocks are shown highlighted. The rest keeps its
  // line breaks and spacing as written.
  let { text, class: className = "" }: { text: string; class?: string } = $props();
  type Part = { code: false; text: string } | { code: true; text: string; language: string };
  const FENCE = /^(`{3,}|~{3,})[ \t]*([\w+#.-]*)[^\n]*\n([\s\S]*?)^\1[ \t]*$/gm;

  function split(source: string): Part[] {
    const parts: Part[] = [];
    let at = 0;
    for (const match of source.matchAll(FENCE)) {
      if (match.index > at) parts.push({ code: false, text: source.slice(at, match.index) });
      parts.push({ code: true, text: match[3].replace(/\n$/, ""), language: match[2] });
      at = match.index + match[0].length;
    }
    if (at < source.length) parts.push({ code: false, text: source.slice(at) });
    return parts;
  }
  const parts = $derived(split(text));
</script>

<!-- On one line: the text keeps its own whitespace, so the markup must add none. -->
<div class={cn("wrap-anywhere whitespace-pre-wrap", className)}>{#each parts as part, index (index)}{#if part.code}<Code text={part.text} language={part.language} focusable class="my-1 overflow-x-auto rounded-md border bg-raised/50 p-2 text-[0.8rem] whitespace-pre" />{:else}{part.text}{/if}{/each}</div>
