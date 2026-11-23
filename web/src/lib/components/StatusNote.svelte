<script lang="ts">
  import CircleAlert from "@lucide/svelte/icons/circle-alert";
  import Inbox from "@lucide/svelte/icons/inbox";
  import type { Snippet } from "svelte";
  import { cn } from "$lib/utils";

  // Empty and error states. Errors are announced (role=alert); empty states are plain text.
  let {
    title,
    detail,
    tone = "empty",
    class: className,
    children,
  }: { title: string; detail?: string; tone?: "empty" | "error"; class?: string; children?: Snippet } = $props();
</script>

<div
  role={tone === "error" ? "alert" : undefined}
  class={cn(
    "flex flex-col items-center gap-2 rounded-xl border border-dashed px-5 py-6 text-center",
    tone === "error" && "border-solid border-coral/45 bg-coral-tint",
    className,
  )}
>
  <span
    class={cn("grid size-9 place-items-center rounded-lg border bg-raised text-signal", tone === "error" && "border-coral/45 bg-transparent text-coral")}
    aria-hidden="true"
  >
    {#if tone === "error"}<CircleAlert class="size-4" />{:else}<Inbox class="size-4" />{/if}
  </span>
  <p class="font-medium">{title}</p>
  {#if detail}<p class="max-w-sm text-sm text-muted-foreground">{detail}</p>{/if}
  {#if children}<div class="mt-1">{@render children()}</div>{/if}
</div>
