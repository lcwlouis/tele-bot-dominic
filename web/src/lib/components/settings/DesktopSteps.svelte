<script lang="ts">
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { untrack } from "svelte";
  import { send, type Permissions } from "$lib/api";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { toast } from "$lib/toast.svelte";

  // The step budget of the desktop tasks the owner asks for. Saved to config.toml with the page's revision, which a
  // save moves, so `revision` is bound to the page's. A task reads it when it starts.
  let {
    revision = $bindable(),
    budget,
    onsaved,
  }: {
    revision: string;
    budget: Permissions["desktop_steps"];
    onsaved: (result: Permissions) => void;
  } = $props();
  // A number input binds a number, or null while it holds something that is not one.
  let draft = $state<number | null>(untrack(() => budget.value)); // the saved value seeds it once
  let saving = $state(false);
  const valid = $derived(Number.isInteger(draft) && draft! >= budget.min && draft! <= budget.max);
  const changed = $derived(valid && draft !== budget.value);

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!changed) return;
    saving = true;
    try {
      const result = await send<Permissions>("/api/permissions/desktop-steps", { revision, max_steps: draft });
      revision = result.revision;
      onsaved(result);
      toast.show("ok", `Desktop tasks now start with ${result.desktop_steps.value} steps.`);
    } catch {
      // send() already showed why
    } finally {
      saving = false;
    }
  }
</script>

<Card.Root class="mt-6">
  <Card.Header>
    <Card.Title class="text-base">Desktop tasks</Card.Title>
    <Card.Description>
      How many steps a desktop task you ask for may take. When one is about to run out, Dom asks you in your DM whether
      to keep going (up to three times this) or wrap up. Dom's own tasks keep the budgets he plans.
    </Card.Description>
  </Card.Header>
  <Card.Content>
    <form class="flex flex-wrap items-end gap-3" onsubmit={save}>
      <div class="grid gap-1.5">
        <label for="desktop-steps" class="text-sm font-medium">Step budget</label>
        <Input
          id="desktop-steps"
          class="w-28"
          type="number"
          inputmode="numeric"
          min={budget.min}
          max={budget.max}
          step="1"
          required
          aria-describedby="desktop-steps-hint"
          aria-invalid={!valid || undefined}
          bind:value={draft}
        />
      </div>
      <Button type="submit" disabled={!changed || saving}>
        {#if saving}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…{:else}Save{/if}
      </Button>
      <p id="desktop-steps-hint" class={["w-full text-sm", valid ? "text-muted-foreground" : "text-coral"]}>
        {valid ? `From ${budget.min} to ${budget.max} steps.` : `Enter a whole number from ${budget.min} to ${budget.max}.`}
      </p>
    </form>
  </Card.Content>
</Card.Root>
