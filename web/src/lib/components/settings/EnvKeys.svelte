<script lang="ts">
  import KeyRound from "@lucide/svelte/icons/key-round";
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { onMount } from "svelte";
  import { getJSON, send } from "$lib/api";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { toast } from "$lib/toast.svelte";

  type EnvKeyRow = { name: string; set: boolean; in_dotenv: boolean };
  type EnvKeysResponse = { keys: EnvKeyRow[] };

  let { onchanged }: { onchanged?: () => void } = $props();

  let keys = $state<EnvKeyRow[] | null>(null);
  let failed = $state("");
  let name = $state("");
  let value = $state("");
  let saving = $state(false);

  async function load() {
    try {
      const data = await getJSON<EnvKeysResponse>("/api/providers/keys");
      keys = data.keys;
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(load);

  const validName = $derived(/^[A-Z_][A-Z0-9_]{0,63}$/.test(name.trim()));
  const canSave = $derived(validName && value.trim().length > 0 && !saving);

  function pick(keyName: string) {
    name = keyName;
    value = "";
  }

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!canSave) return;
    saving = true;
    try {
      const data = await send<EnvKeysResponse>("/api/providers/keys", {
        name: name.trim(),
        value: value.trim(),
      });
      keys = data.keys;
      toast.show("ok", `${name.trim()} saved to .env.`);
      value = "";
      onchanged?.();
    } catch {
      // send() already showed why
    } finally {
      saving = false;
    }
  }
</script>

<section aria-labelledby="env-keys-title" class="mt-8">
  <Card.Root>
    <Card.Header>
      <div class="flex items-center gap-2">
        <KeyRound class="size-4 text-muted-foreground" aria-hidden="true" />
        <Card.Title id="env-keys-title" class="text-lg">API keys (.env)</Card.Title>
      </div>
      <Card.Description>
        Add or replace API keys in .env without SSH. Existing values are write-only and never shown here. Applies at once.
      </Card.Description>
    </Card.Header>
    <Card.Content class="grid gap-5">
      {#if failed && keys === null}
        <StatusNote tone="error" title="Couldn't load key names" detail={`Dom's dashboard answered: ${failed}.`}>
          <Button variant="secondary" onclick={load}>Retry</Button>
        </StatusNote>
      {:else if keys === null}
        <p role="status" class="text-sm text-muted-foreground">Loading…</p>
      {:else if keys.length}
        <ul class="divide-y rounded-xl border">
          {#each keys as row (row.name)}
            <li class="flex flex-wrap items-center justify-between gap-2 px-3.5 py-2.5 text-sm">
              <span class="font-mono break-all">{row.name}</span>
              <div class="flex items-center gap-2">
                <Badge variant={row.set ? "secondary" : "destructive"}>{row.set ? "Set" : "Missing"}</Badge>
                <Button variant="ghost" size="xs" onclick={() => pick(row.name)}>
                  {row.set ? "Replace" : "Set"}
                </Button>
              </div>
            </li>
          {/each}
        </ul>
      {/if}

      <form class="grid gap-3 md:grid-cols-[1fr_1.4fr_auto] md:items-end" onsubmit={save}>
        <div class="grid gap-1.5">
          <label for="env-key-name" class="text-sm font-medium">Variable name</label>
          <Input id="env-key-name" type="text" placeholder="ANTHROPIC_API_KEY" bind:value={name} />
        </div>
        <div class="grid gap-1.5">
          <label for="env-key-value" class="text-sm font-medium">Secret value</label>
          <Input
            id="env-key-value"
            type="password"
            autocomplete="off"
            placeholder="Paste key (never shown again)"
            bind:value={value}
          />
        </div>
        <Button type="submit" disabled={!canSave}>
          {#if saving}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…{:else}Save key{/if}
        </Button>
      </form>
    </Card.Content>
  </Card.Root>
</section>
