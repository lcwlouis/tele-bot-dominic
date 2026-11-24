<script lang="ts">
  import Code from "$lib/components/code/Code.svelte";
  import type { Token } from "$lib/markdown";
  import Blocks from "./Blocks.svelte";
  import Inline from "./Inline.svelte";

  // Block markdown as elements. A raw HTML block shows as its source in a code block, never as markup.
  // `tight`: a tight list item, whose text sits in the item without a paragraph.
  let { tokens, tight = false }: { tokens: Token[]; tight?: boolean } = $props();
  const align = (value: string | null) => (value === "center" ? "text-center" : value === "right" ? "text-right" : undefined);
</script>

{#each tokens as token, index (index)}
  {#if token.type === "heading"}
    <svelte:element this={`h${Math.min(token.depth + 1, 6)}`} data-depth={token.depth}><Inline tokens={token.tokens ?? []} /></svelte:element>
  {:else if token.type === "paragraph"}<p><Inline tokens={token.tokens ?? []} /></p>
  {:else if token.type === "code"}<Code text={token.text} language={token.lang || null} focusable />
  {:else if token.type === "html"}<Code text={token.raw.trimEnd()} language="xml" focusable />
  {:else if token.type === "blockquote"}<blockquote><Blocks tokens={token.tokens ?? []} /></blockquote>
  {:else if token.type === "hr"}<hr />
  {:else if token.type === "list"}
    <svelte:element this={token.ordered ? "ol" : "ul"} start={token.ordered && token.start !== 1 && token.start !== "" ? token.start : undefined}>
      {#each token.items as item, at (at)}
        <li class={item.task ? "task" : undefined}>
          <Blocks tokens={item.tokens} tight={!item.loose} />
        </li>
      {/each}
    </svelte:element>
  {:else if token.type === "table"}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div class="md-table" tabindex="0" role="region" aria-label="Table">
      <table>
        <thead>
          <tr>{#each token.header as cell, at (at)}<th class={align(cell.align)}><Inline tokens={cell.tokens} /></th>{/each}</tr>
        </thead>
        <tbody>
          {#each token.rows as row, r (r)}
            <tr>{#each row as cell, at (at)}<td class={align(cell.align)}><Inline tokens={cell.tokens} /></td>{/each}</tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if token.type === "checkbox"}<input type="checkbox" checked={token.checked} disabled aria-label={token.checked ? "Done" : "Not done"} />{" "}
  {:else if token.type === "text" && tight}<Inline tokens={token.tokens ?? [token]} />
  {:else if token.type === "text"}<p><Inline tokens={token.tokens ?? [token]} /></p>
  {/if}
{/each}
