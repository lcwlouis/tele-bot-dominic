<script lang="ts">
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";

  // The network and storage notices every plugin listing shows, in the server's own words.
  let {
    network,
    storage,
    notices,
    storageOff = false,
  }: { network: boolean; storage: boolean; notices: { network: string; storage: string }; storageOff?: boolean } = $props();
</script>

{#if network}
  <p class="flex items-start gap-2 text-sm text-amber"><TriangleAlert class="mt-0.5 size-4 shrink-0" aria-hidden="true" />{notices.network}</p>
{/if}
{#if storage}
  <p class="flex items-start gap-2 text-sm text-amber"><TriangleAlert class="mt-0.5 size-4 shrink-0" aria-hidden="true" />{notices.storage}</p>
{:else if storageOff}
  <p class="text-sm text-muted-foreground">Storage off</p>
{/if}
