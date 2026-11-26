<script lang="ts">
  import CircleAlert from "@lucide/svelte/icons/circle-alert";
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import RotateCw from "@lucide/svelte/icons/rotate-cw";
  import { onDestroy } from "svelte";
  import { ApiError, getJSON, send, type Health, type RestartResult } from "$lib/api";
  import * as AlertDialog from "$lib/components/ui/alert-dialog";
  import { Button } from "$lib/components/ui/button";
  import { cn } from "$lib/utils";

  // "Restart Dom to apply", with the button that does it (E85). The server refuses (409, with its reason) while a
  // desktop task or a card would be lost; otherwise Dom stops gracefully and exits 75, and his service manager starts
  // him again. The page then asks /api/health until a new process (another `boot`) answers, and reloads.
  let { title, detail, class: className }: { title: string; detail: string; class?: string } = $props();

  const POLL_MS = 1500;
  const GIVE_UP_MS = 180_000; // three minutes: past this, something other than a restart is wrong

  let confirming = $state(false);
  let phase = $state<"idle" | "asking" | "restarting" | "slow">("idle");
  let refusal = $state("");
  let timer: ReturnType<typeof setTimeout> | undefined;

  onDestroy(() => clearTimeout(timer));

  async function restart() {
    confirming = false;
    phase = "asking";
    refusal = "";
    try {
      const result = await send<RestartResult>("/api/system/restart", {}, { quiet: true });
      phase = "restarting";
      wait(result.boot, Date.now());
    } catch (error) {
      phase = "idle";
      refusal = error instanceof ApiError ? error.message : "Couldn't restart Dom. Try again.";
    }
  }

  /** Ask until another process answers. A failed request means Dom is still down, which is expected. */
  function wait(boot: string, since: number) {
    timer = setTimeout(async () => {
      try {
        const health = await getJSON<Health>("/api/health");
        if (health.boot !== boot) {
          location.reload();
          return;
        }
      } catch {
        // still stopping or starting
      }
      if (Date.now() - since > GIVE_UP_MS) phase = "slow";
      wait(boot, since);
    }, POLL_MS);
  }
</script>

<div
  role="region"
  aria-labelledby="restart-title"
  class={cn("flex flex-wrap items-center gap-x-4 gap-y-3 rounded-xl border border-coral/45 bg-coral-tint px-4 py-3.5", className)}
>
  <CircleAlert class="size-4 shrink-0 text-coral" aria-hidden="true" />
  <div class="min-w-0 flex-1 basis-56">
    <p id="restart-title" class="font-medium">{title}</p>
    <p class="text-sm text-muted-foreground">{detail}</p>
  </div>
  {#if phase === "restarting" || phase === "slow"}
    <p role="status" class="flex items-center gap-2 text-sm font-medium">
      <LoaderCircle class="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" /> Restarting…
    </p>
  {:else}
    <Button id="restart-dom" variant="secondary" onclick={() => (confirming = true)} disabled={phase === "asking"}>
      <RotateCw data-icon="inline-start" aria-hidden="true" /> Restart Dom
    </Button>
  {/if}
  {#if refusal}
    <p role="alert" class="basis-full text-sm text-coral">Not restarted: {refusal}</p>
  {/if}
  {#if phase === "slow"}
    <p role="alert" class="basis-full text-sm text-coral">
      Dom hasn't come back after three minutes. Check his service on the machine he runs on; this page keeps asking.
    </p>
  {/if}
</div>

<AlertDialog.Root bind:open={confirming}>
  <AlertDialog.Content>
    <AlertDialog.Header>
      <AlertDialog.Title>Restart Dom now?</AlertDialog.Title>
      <AlertDialog.Description>
        Dom finishes what he is saying, stops, and his service starts him again. Replies pause for about a minute.
        This page reloads when he is back.
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel variant="outline">Not now</AlertDialog.Cancel>
      <AlertDialog.Action onclick={restart}>Restart</AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>
