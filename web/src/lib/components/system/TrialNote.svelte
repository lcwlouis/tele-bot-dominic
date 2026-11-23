<script lang="ts">
  import type { Trial } from "$lib/api";

  // A plugin's test run, exactly as the old pages showed it. The detail is the plugin's own output: plain text only.
  let { trial, pending = false }: { trial: Trial | null; pending?: boolean } = $props();
</script>

{#if trial}
  <details open={trial.failed} class="rounded-lg border bg-raised/50 px-3 py-2 text-sm">
    <summary class="cursor-pointer font-medium {trial.failed ? 'text-coral' : ''}">
      Test run {trial.failed ? "failed" : "passed"} · {trial.label} · {trial.elapsed_ms} ms
    </summary>
    <p class="mt-2 font-mono text-[0.8rem] break-words whitespace-pre-wrap text-muted-foreground">{trial.detail}</p>
  </details>
{:else}
  <p class="text-sm text-muted-foreground">Test run {pending ? "pending" : "unavailable"}.</p>
{/if}
