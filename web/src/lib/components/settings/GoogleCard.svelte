<script lang="ts">
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { onMount } from "svelte";
  import { getJSON, send, type GoogleView } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import { Switch } from "$lib/components/ui/switch";
  import { toast } from "$lib/toast.svelte";
  import { cn } from "$lib/utils";
  import { SERVICE_STATE } from "./ServiceCard.svelte";

  // Settings → Services → Google (v0.8.5 W4): connect, the four switches and disconnect. The client id and secret only
  // ever show as set or not set. Connecting leaves for Google and comes back to Settings with ?google=connected|failed.
  const PATH = "/api/integrations/google";
  let view = $state<GoogleView | null>(null);
  let failed = $state("");
  let busy = $state("");

  const health = $derived.by(() => {
    if (!view) return "off" as const;
    if (view.status === "needs_reconnect") return "error" as const;
    if (view.status === "connected") return "ok" as const;
    return view.client_id_set && view.client_secret_set && view.redirect_uri ? ("off" as const) : ("missing" as const);
  });
  const look = $derived(SERVICE_STATE[health]);
  const detail = $derived.by(() => {
    if (!view) return "";
    if (view.status === "connected") return "Connected. Dom can use only what the switches below allow, in your DM.";
    if (view.status === "needs_reconnect") return view.last_error || "Google access expired or was revoked. Connect again.";
    if (!view.client_id_set || !view.client_secret_set) return "Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env, then restart Dom.";
    if (!view.redirect_uri) return "Set web.public_url to the dashboard's https address first.";
    return view.last_error || "Not connected. Dom cannot see your calendar or mail.";
  });

  async function load() {
    try {
      view = await getJSON<GoogleView>(PATH);
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(() => {
    // Google's redirect lands here: say how it went, then drop the marker from the address.
    const params = new URLSearchParams(location.search);
    const outcome = params.get("google");
    if (outcome) {
      params.delete("google");
      history.replaceState(null, "", location.pathname + (params.size ? `?${params}` : ""));
    }
    void load().then(() => {
      if (outcome === "connected") toast.show("ok", "Google connected. Turn on what Dom may use.");
      else if (outcome === "failed") toast.show("error", view?.last_error || "Connecting Google failed.");
      else if (outcome === "invalid") toast.show("error", "That connect link was not valid or has expired. Press Connect Google again.");
    });
  });

  async function connect() {
    busy = "connect";
    try {
      const { url } = await send<{ url: string }>(`${PATH}/connect`, {});
      location.assign(url);
    } catch {
      busy = ""; // the toast says why
    }
  }

  async function disconnect() {
    busy = "disconnect";
    try {
      const result = await send<GoogleView & { revoked: boolean }>(`${PATH}/disconnect`, {});
      view = result;
      toast.show("ok", result.revoked ? "Google disconnected and the token revoked." : "Disconnected here. Google did not confirm the revoke: remove Dom in your Google account too.");
    } catch {
      // the toast says why
    } finally {
      busy = "";
    }
  }

  async function toggle(id: string, on: boolean) {
    busy = id;
    try {
      view = await send<GoogleView>(`${PATH}/switch`, { id, on });
    } catch {
      // the toast says why; the switch falls back to what the server holds
      await load();
    } finally {
      busy = "";
    }
  }
</script>

<section aria-labelledby="google-title" class="flex flex-col gap-3">
  <div class="flex flex-wrap items-start gap-x-3 gap-y-1">
    <span class={cn("mt-0.5 inline-flex items-center gap-1.5", look.tone)} data-state={health}>
      <look.icon class="size-4" aria-hidden="true" />
      <span class="sr-only">{look.label}:</span>
    </span>
    <div class="min-w-0 flex-1">
      <div class="flex flex-wrap items-center gap-1.5">
        <h3 id="google-title" class="text-base font-semibold">Google Calendar and Gmail</h3>
        <InfoGlyph label="About Google">
          Dom reads your calendar and mail and can save drafts, only in your DM. Every change to your calendar and every draft
          asks you first, and Dom never sends mail. Each switch is off until you turn it on.
        </InfoGlyph>
        <Badge variant="outline" class={look.tone}>{look.label}</Badge>
      </div>
      {#if failed && !view}
        <p class="mt-0.5 text-sm text-coral">Couldn't load Google's status ({failed}).</p>
      {:else}
        <p class="mt-0.5 text-sm break-words text-muted-foreground">{detail}</p>
      {/if}
    </div>
  </div>

  {#if view}
    <ul class="flex flex-wrap gap-1.5 text-sm" aria-label="Google credentials in .env">
      <li><Badge variant={view.client_id_set ? "secondary" : "outline"}>Client ID: {view.client_id_set ? "set" : "not set"}</Badge></li>
      <li><Badge variant={view.client_secret_set ? "secondary" : "outline"}>Client secret: {view.client_secret_set ? "set" : "not set"}</Badge></li>
    </ul>
    {#if view.redirect_uri}
      <p class="text-sm">Redirect URI for the Google client: <code class="rounded bg-raised px-1.5 py-0.5 font-mono text-xs break-all">{view.redirect_uri}</code></p>
    {/if}

    {#if view.status === "connected"}
      <ul class="grid gap-2" aria-label="What Dom may use">
        {#each view.switches as item (item.id)}
          <li class="flex items-center justify-between gap-3">
            <label for={`google-${item.id}`} class="text-sm">{item.label}</label>
            <Switch
              id={`google-${item.id}`}
              checked={item.on}
              disabled={!item.granted || busy !== ""}
              onCheckedChange={(on) => toggle(item.id, on)}
            />
          </li>
        {/each}
      </ul>
      {#if view.switches.some((item) => !item.granted)}
        <p class="text-xs text-muted-foreground">A switch is locked when you did not tick that permission on Google's screen. Connect again to grant it.</p>
      {/if}
    {/if}

    <div class="flex flex-wrap items-center gap-2">
      <Button
        type="button"
        size="sm"
        onclick={connect}
        disabled={busy !== "" || !view.client_id_set || !view.client_secret_set || !view.redirect_uri}
      >
        {#if busy === "connect"}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Opening Google…{:else}{view.status === "disconnected" ? "Connect Google" : "Connect again"}{/if}
      </Button>
      {#if view.status !== "disconnected"}
        <Button type="button" variant="ghost" size="sm" onclick={disconnect} disabled={busy !== ""}>
          {#if busy === "disconnect"}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Disconnecting…{:else}Disconnect Google{/if}
        </Button>
      {/if}
    </div>
  {/if}
</section>
