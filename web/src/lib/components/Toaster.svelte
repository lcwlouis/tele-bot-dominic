<script lang="ts">
  import Check from "@lucide/svelte/icons/check";
  import TriangleAlert from "@lucide/svelte/icons/triangle-alert";
  import { toast } from "$lib/toast.svelte";
</script>

<div class="pointer-events-none fixed inset-x-0 bottom-24 z-50 flex justify-center px-4 lg:bottom-6" role="status" aria-live="polite">
  {#if toast.current}
    <p
      class="pointer-events-auto flex max-w-lg items-center gap-2.5 rounded-xl border bg-popover px-4 py-3 text-sm shadow-lg {toast.current.tone === 'error' ? 'border-coral/50' : 'border-signal/40'}"
    >
      {#if toast.current.tone === "error"}
        <TriangleAlert class="size-4 shrink-0 text-coral" aria-hidden="true" />
      {:else}
        <Check class="size-4 shrink-0 text-signal" aria-hidden="true" />
      {/if}
      {toast.current.text}
    </p>
  {/if}
</div>
