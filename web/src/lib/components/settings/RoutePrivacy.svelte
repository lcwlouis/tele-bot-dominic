<script lang="ts">
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import { endpointHostname, isDeepSeekChat, ZDR_RECOMMENDATION } from "./roles";

  // A cloud route's privacy note. DeepSeek keeps data, so it is a warning and stays as text; the general zero data
  // retention recommendation is shown once at setup and is a glyph here (E80).

  let { url, local, class: className = "" }: { url: string; local?: boolean; class?: string } = $props();
  const hostname = $derived(endpointHostname(url));
  // Saved routes use the server's classification. In the editor, names and
  // addresses conventionally used for local servers don't need a cloud warning.
  const onLocalNetwork = $derived(local ?? (
    !hostname.includes(".") || /\.(local|lan|internal|home\.arpa)$/.test(hostname)
    || /^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(hostname)
  ));
</script>

{#if hostname && !onLocalNetwork && hostname !== "openrouter.ai" && !hostname.endsWith(".openrouter.ai")}
  {#if isDeepSeekChat(url)}
    <p class="text-xs text-coral {className}">
      DeepSeek keeps inputs in China and may use them for training unless you opt out.
      <a class="underline underline-offset-2" href="https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html" target="_blank" rel="noopener noreferrer">Privacy policy</a>. {ZDR_RECOMMENDATION}
    </p>
  {:else}
    <p class="flex items-center gap-1 text-xs text-muted-foreground {className}">
      Cloud provider
      <InfoGlyph label="Data retention" class="size-5">
        Review this cloud provider's data retention and training terms. {ZDR_RECOMMENDATION}
      </InfoGlyph>
    </p>
  {/if}
{/if}
