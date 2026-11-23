<script lang="ts">
  import { onMount } from "svelte";
  import { getJSON, type ChatMessages } from "$lib/api";
  import { Button } from "$lib/components/ui/button";
  import { cn } from "$lib/utils";

  // One chat's recent messages. The owner's DM comes in full; any other chat comes as one line per message.
  let { chatId, title }: { chatId: number; title: string } = $props();

  let data = $state<ChatMessages | null>(null);
  let failed = $state("");

  async function load() {
    failed = "";
    try {
      data = await getJSON<ChatMessages>(`/api/chats/${chatId}/messages`);
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }
  onMount(load);
</script>

<section aria-label={`Recent messages in ${title}`} class="grid gap-3 border-t pt-4">
  <div class="flex flex-wrap items-baseline justify-between gap-2">
    <p class="text-sm font-medium">Recent messages</p>
    <a href={`/app/system/turns?chat=${chatId}`} class="text-sm font-medium text-signal-strong underline-offset-4 hover:underline">
      Turn log for this chat
    </a>
  </div>
  {#if failed}
    <p role="alert" class="text-sm text-coral">Couldn't load the messages: {failed}. <Button variant="link" size="xs" onclick={load}>Retry</Button></p>
  {:else if data === null}
    <div class="h-24 animate-pulse rounded-lg bg-raised/60" aria-hidden="true"></div>
    <p class="sr-only" role="status">Loading messages…</p>
  {:else if data.messages.length === 0}
    <p class="text-sm text-muted-foreground">No messages yet.</p>
  {:else}
    {#if !data.full}
      <p class="text-xs text-muted-foreground">One line per message: the dashboard shows only your own DM in full.</p>
    {/if}
    <!-- It scrolls by itself, so it takes keyboard focus to scroll from the keyboard. -->
    <ol class="grid max-h-96 gap-2 overflow-y-auto rounded-lg outline-none focus-visible:ring-3 focus-visible:ring-ring/50" tabindex="0" aria-label={`Messages in ${title}`}>
      {#each data.messages as message, index (index)}
        <li class={cn("min-w-0 rounded-lg border px-3 py-2 text-sm", message.mine ? "border-signal/30 bg-tint" : "bg-raised/40")}>
          <p class="flex justify-between gap-3 text-xs text-muted-foreground">
            <span class="font-medium text-foreground">{message.who}</span><span class="font-mono">{message.at}</span>
          </p>
          <p class={cn("mt-0.5 break-words [overflow-wrap:anywhere]", data.full ? "whitespace-pre-line" : "truncate")}>{message.text}</p>
        </li>
      {/each}
    </ol>
  {/if}
</section>
