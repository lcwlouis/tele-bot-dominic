<script lang="ts">
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { untrack } from "svelte";
  import { send, type Permissions, type ReplyControls } from "$lib/api";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { toast } from "$lib/toast.svelte";

  // Triage confidence and bot-to-bot loop limits (`[dispatch]` in config.toml). Dispatch reads them on every turn,
  // so saving applies immediately without a restart.
  let {
    revision = $bindable(),
    controls,
    onsaved,
  }: {
    revision: string;
    controls: ReplyControls;
    onsaved: (result: Permissions) => void;
  } = $props();

  let confidence = $state<number | null>(untrack(() => controls.triage_confidence.value));
  let botDepth = $state<number | null>(untrack(() => controls.bot_depth_limit.value));
  let botInterval = $state<number | null>(untrack(() => controls.bot_reply_interval_seconds.value));
  let saving = $state(false);

  const confidenceValid = $derived(
    typeof confidence === "number" &&
      Number.isFinite(confidence) &&
      confidence >= controls.triage_confidence.min &&
      confidence <= controls.triage_confidence.max,
  );
  const depthValid = $derived(
    Number.isInteger(botDepth) &&
      botDepth! >= controls.bot_depth_limit.min &&
      botDepth! <= controls.bot_depth_limit.max,
  );
  const intervalValid = $derived(
    Number.isInteger(botInterval) &&
      botInterval! >= controls.bot_reply_interval_seconds.min &&
      botInterval! <= controls.bot_reply_interval_seconds.max,
  );
  const valid = $derived(confidenceValid && depthValid && intervalValid);
  const changed = $derived(
    valid &&
      (confidence !== controls.triage_confidence.value ||
        botDepth !== controls.bot_depth_limit.value ||
        botInterval !== controls.bot_reply_interval_seconds.value),
  );

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!changed) return;
    saving = true;
    try {
      const result = await send<Permissions>("/api/permissions/reply-controls", {
        revision,
        triage_confidence: confidence,
        bot_depth_limit: botDepth,
        bot_reply_interval_seconds: botInterval,
      });
      revision = result.revision;
      confidence = result.reply_controls.triage_confidence.value;
      botDepth = result.reply_controls.bot_depth_limit.value;
      botInterval = result.reply_controls.bot_reply_interval_seconds.value;
      onsaved(result);
      toast.show("ok", "Reply and bot-loop settings saved.");
    } catch {
      // send() already showed why
    } finally {
      saving = false;
    }
  }
</script>

<Card.Root class="mt-6">
  <Card.Header>
    <Card.Title class="text-base">Triage &amp; bot loops</Card.Title>
    <Card.Description>
      How readily Jev lets Dom stay quiet or react-only without calling the chat model, and how quickly Dom stops
      answering another bot. Applies at once.
    </Card.Description>
  </Card.Header>
  <Card.Content>
    <form class="grid gap-4" onsubmit={save}>
      <div class="grid gap-4 sm:grid-cols-3">
        <div class="grid content-start gap-1.5">
          <label for="triage-confidence" class="text-sm font-medium">Jev confidence</label>
          <Input
            id="triage-confidence"
            class="w-28"
            type="number"
            inputmode="decimal"
            min={controls.triage_confidence.min}
            max={controls.triage_confidence.max}
            step="any"
            required
            aria-describedby="triage-confidence-hint"
            aria-invalid={!confidenceValid || undefined}
            bind:value={confidence}
          />
          <p id="triage-confidence-hint" class={["text-xs", confidenceValid ? "text-muted-foreground" : "text-coral"]}>
            {confidenceValid
              ? `From ${controls.triage_confidence.min} to ${controls.triage_confidence.max}. Lower stays quiet or reacts more often.`
              : `Enter a number from ${controls.triage_confidence.min} to ${controls.triage_confidence.max}.`}
          </p>
        </div>

        <div class="grid content-start gap-1.5">
          <label for="bot-depth-limit" class="text-sm font-medium">Bot-to-bot reply cap</label>
          <Input
            id="bot-depth-limit"
            class="w-28"
            type="number"
            inputmode="numeric"
            min={controls.bot_depth_limit.min}
            max={controls.bot_depth_limit.max}
            step="1"
            required
            aria-describedby="bot-depth-limit-hint"
            aria-invalid={!depthValid || undefined}
            bind:value={botDepth}
          />
          <p id="bot-depth-limit-hint" class={["text-xs", depthValid ? "text-muted-foreground" : "text-coral"]}>
            {depthValid
              ? `From ${controls.bot_depth_limit.min} to ${controls.bot_depth_limit.max} bot-only turns before Dom bows out.`
              : `Enter a whole number from ${controls.bot_depth_limit.min} to ${controls.bot_depth_limit.max}.`}
          </p>
        </div>

        <div class="grid content-start gap-1.5">
          <label for="bot-reply-interval" class="text-sm font-medium">Bot cooldown (s)</label>
          <Input
            id="bot-reply-interval"
            class="w-28"
            type="number"
            inputmode="numeric"
            min={controls.bot_reply_interval_seconds.min}
            max={controls.bot_reply_interval_seconds.max}
            step="1"
            required
            aria-describedby="bot-reply-interval-hint"
            aria-invalid={!intervalValid || undefined}
            bind:value={botInterval}
          />
          <p id="bot-reply-interval-hint" class={["text-xs", intervalValid ? "text-muted-foreground" : "text-coral"]}>
            {intervalValid
              ? `From ${controls.bot_reply_interval_seconds.min} to ${controls.bot_reply_interval_seconds.max} seconds between replies to the same bot.`
              : `Enter a whole number from ${controls.bot_reply_interval_seconds.min} to ${controls.bot_reply_interval_seconds.max}.`}
          </p>
        </div>
      </div>

      <div>
        <Button type="submit" disabled={!changed || saving}>
          {#if saving}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…{:else}Save{/if}
        </Button>
      </div>
    </form>
  </Card.Content>
</Card.Root>
