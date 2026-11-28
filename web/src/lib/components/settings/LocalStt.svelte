<script lang="ts">
  import Activity from "@lucide/svelte/icons/activity";
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { onMount, untrack } from "svelte";
  import { getJSON, send, type SttView, type TestResult } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { toast } from "$lib/toast.svelte";

  // Local OpenAI-compatible speech-to-text (`[stt]` in config.toml). Voice notes, audio files and video-note
  // soundtracks try this server first when the chat model does not accept audio natively, and fall back to the
  // media model if it is unreachable or fails.
  let { revision = $bindable() }: { revision: string } = $props();

  let view = $state<SttView | null>(null);
  let baseUrl = $state("");
  let model = $state("whisper-1");
  let language = $state("");
  let apiKeyEnv = $state("");
  let failed = $state("");
  let saving = $state(false);
  let testing = $state(false);
  let testResult = $state<TestResult | null>(null);

  function sync(loaded: SttView) {
    view = loaded;
    baseUrl = loaded.base_url;
    model = loaded.model;
    language = loaded.language;
    apiKeyEnv = loaded.api_key_env;
  }

  async function load() {
    try {
      sync(await getJSON<SttView>("/api/media/stt"));
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(load);

  $effect(() => {
    const current = revision;
    untrack(() => {
      if (view && current && current !== view.revision && !saving) void load();
    });
  });

  const changed = $derived(
    view !== null &&
      (baseUrl.trim() !== view.base_url ||
        model.trim() !== view.model ||
        language.trim() !== view.language ||
        apiKeyEnv.trim() !== view.api_key_env),
  );

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!view || !changed) return;
    saving = true;
    testResult = null;
    try {
      const saved = await send<SttView>("/api/media/stt", {
        revision,
        base_url: baseUrl.trim(),
        model: model.trim() || "whisper-1",
        language: language.trim(),
        api_key_env: apiKeyEnv.trim(),
      });
      revision = saved.revision;
      sync(saved);
      toast.show("ok", saved.base_url ? "Local speech-to-text saved." : "Local speech-to-text turned off.");
    } catch {
      // send() already showed why
    } finally {
      saving = false;
    }
  }

  async function runTest() {
    if (!baseUrl.trim() || testing) return;
    testing = true;
    testResult = null;
    try {
      testResult = await send<TestResult>("/api/media/stt/test", {
        base_url: baseUrl.trim(),
        model: model.trim() || "whisper-1",
        language: language.trim(),
        api_key_env: apiKeyEnv.trim(),
      });
    } catch {
      // send() already showed why
    } finally {
      testing = false;
    }
  }
</script>

<Card.Root role="region" aria-labelledby="local-stt-title">
  <Card.Header>
    <div class="flex items-center gap-1.5">
      <Card.Title id="local-stt-title" class="text-lg">Local speech-to-text</Card.Title>
      <InfoGlyph label="About local speech-to-text">
        An OpenAI-compatible /audio/transcriptions server (such as mlx-audio, whisper.cpp or faster-whisper-server).
        Voice notes, audio files and video-note soundtracks try this first when your chat model does not take audio
        natively, and fall back to the media model if it is offline.
      </InfoGlyph>
    </div>
    <Card.Description>
      Leave the address empty to transcribe with the media model only. Applies at once.
    </Card.Description>
  </Card.Header>
  <Card.Content>
    {#if failed && !view}
      <StatusNote tone="error" title="Couldn't load speech-to-text" detail={`Dom's dashboard answered: ${failed}.`}>
        <Button variant="secondary" onclick={load}>Retry</Button>
      </StatusNote>
    {:else if !view}
      <p role="status" class="text-sm text-muted-foreground">Loading…</p>
    {:else}
      <form class="grid gap-4" onsubmit={save}>
        <div class="grid gap-4 md:grid-cols-2">
          <div class="grid content-start gap-1.5">
            <label for="stt-base-url" class="text-sm font-medium">Server address</label>
            <Input
              id="stt-base-url"
              type="url"
              placeholder="http://192.168.68.24:8000/v1"
              bind:value={baseUrl}
            />
            <p class="text-xs text-muted-foreground">Base URL of the server; `/audio/transcriptions` is appended.</p>
          </div>
          <div class="grid content-start gap-1.5">
            <label for="stt-model" class="text-sm font-medium">Model</label>
            <Input
              id="stt-model"
              type="text"
              placeholder="whisper-1"
              bind:value={model}
            />
            <p class="text-xs text-muted-foreground">Model ID sent to the server (e.g. mlx-community/whisper-large-v3-turbo).</p>
          </div>
          <div class="grid content-start gap-1.5">
            <label for="stt-language" class="text-sm font-medium">Language (optional)</label>
            <Input
              id="stt-language"
              type="text"
              placeholder="Auto-detect"
              maxlength={16}
              bind:value={language}
            />
            <p class="text-xs text-muted-foreground">ISO-639-1 code (e.g. en). Leave empty for mixed languages.</p>
          </div>
          <div class="grid content-start gap-1.5">
            <label for="stt-api-key-env" class="text-sm font-medium">API key variable (optional)</label>
            <Input
              id="stt-api-key-env"
              type="text"
              placeholder="STT_API_KEY"
              bind:value={apiKeyEnv}
            />
            <p class="text-xs text-muted-foreground">
              Optional `STT_*` var in .env if your local server needs a bearer token.
              {#if view.api_key_env}
                <Badge variant={view.key_set ? "secondary" : "destructive"} class="ml-1">
                  {view.key_set ? "Key set" : "Key missing"}
                </Badge>
              {/if}
            </p>
          </div>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <Button type="submit" disabled={!changed || saving}>
            {#if saving}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…{:else}Save{/if}
          </Button>
          <Button type="button" variant="secondary" onclick={runTest} disabled={!baseUrl.trim() || testing}>
            {#if testing}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Testing…{:else}<Activity data-icon="inline-start" aria-hidden="true" /> Test{/if}
          </Button>
          {#if testResult}
            <p class="text-sm" aria-live="polite">
              {#if testResult.ok}
                <Badge>OK · {(testResult.latency_ms / 1000).toFixed(1)}s</Badge>
                <span class="ml-1 text-muted-foreground">{testResult.reply}</span>
              {:else}
                <span class="text-coral">{(testResult.latency_ms / 1000).toFixed(1)}s · {testResult.error}</span>
              {/if}
            </p>
          {/if}
        </div>
      </form>
    {/if}
  </Card.Content>
</Card.Root>
