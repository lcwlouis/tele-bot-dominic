<script lang="ts">
  import { decode, safeHref, type Token } from "$lib/markdown";
  import Inline from "./Inline.svelte";

  // Inline markdown as elements and text. Raw HTML tags show as their literal text; a link with an unsafe or
  // relative target shows as its words alone; an image shows as its alt text, since the CSP loads no other host.
  let { tokens }: { tokens: Token[] } = $props();
</script>

{#each tokens as token, index (index)}
  {#if token.type === "strong"}<strong><Inline tokens={token.tokens ?? []} /></strong>
  {:else if token.type === "em"}<em><Inline tokens={token.tokens ?? []} /></em>
  {:else if token.type === "del"}<del><Inline tokens={token.tokens ?? []} /></del>
  {:else if token.type === "codespan"}<code>{token.text}</code>
  {:else if token.type === "br"}<br />
  {:else if token.type === "link"}
    {@const href = safeHref(token.href)}
    {#if href}<a {href} target="_blank" rel="noopener noreferrer" title={token.title || undefined}><Inline tokens={token.tokens ?? []} /></a>
    {:else}<Inline tokens={token.tokens ?? []} />{/if}
  {:else if token.type === "image"}{decode(token.text)}
  {:else if token.type === "html"}{token.raw}
  {:else if "tokens" in token && token.tokens?.length}<Inline tokens={token.tokens} />
  {:else if "text" in token}{decode(token.text)}
  {/if}
{/each}
