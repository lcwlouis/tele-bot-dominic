<script lang="ts">
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import { onMount } from "svelte";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import { getJSON, send, type GoogleCalendar, type GoogleFiles, type GoogleView } from "$lib/api";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import BookingWindows from "./BookingWindows.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import * as AlertDialog from "$lib/components/ui/alert-dialog";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import { Switch } from "$lib/components/ui/switch";
  import { toast } from "$lib/toast.svelte";
  import { cn } from "$lib/utils";
  import { SERVICE_STATE } from "./ServiceCard.svelte";

  // Settings → Services → Google (v0.8.5 W4, v0.8.6 W1 and W4): connect, the switches, Dom's own calendars, his Drive files and disconnect. The client id and secret only
  // ever show as set or not set. Connecting leaves for Google and comes back to Settings with ?google=connected|failed.
  const PATH = "/api/integrations/google";
  let view = $state<GoogleView | null>(null);
  let failed = $state("");
  let busy = $state("");
  let calendars = $state<GoogleCalendar[] | null>(null);
  let removing = $state<GoogleCalendar | null>(null);
  let files = $state<GoogleFiles | null>(null);

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

  // Dom's own calendars come from Google on their own request, so the card itself never waits on Google.
  async function loadCalendars() {
    calendars = null;
    if (!view?.own_calendars_granted) return;
    try {
      calendars = (await getJSON<{ calendars: GoogleCalendar[] }>(`${PATH}/calendars`)).calendars;
    } catch {
      calendars = view.calendars.map((item) => ({ ...item, color: "", upcoming: null, more: false }));
    }
  }

  // The files Dom may see come from Google on their own request too (names only).
  async function loadFiles() {
    files = null;
    const granted = view?.switches.find((item) => item.id === "drive_files")?.granted;
    if (!granted) return;
    try {
      files = await getJSON<GoogleFiles>(`${PATH}/files`);
    } catch {
      files = { folder: null, files: [], more: false };
    }
  }

  async function removeCalendar(item: GoogleCalendar) {
    busy = `calendar-${item.id}`;
    try {
      view = await send<GoogleView>(`${PATH}/calendars/remove`, { id: item.id });
      toast.show("ok", `Removed ${item.name} from Google Calendar.`);
      await loadCalendars();
    } catch {
      // the toast says why
    } finally {
      busy = "";
      removing = null;
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
      void loadCalendars();
      void loadFiles();
      if (outcome === "connected") toast.show("ok", "Google connected. Turn on what Dom may use.");
      else if (outcome === "failed") toast.show("error", view?.last_error || "Connecting Google failed.");
      else if (outcome === "invalid") toast.show("error", "That connect link was not valid or has expired. Press Connect Google again.");
    });
  });

  // `also` names the switches whose permission this connect should ask Google for (a newer permission is asked only when wanted).
  async function connect(also: string[] = []) {
    busy = also.length ? `connect-${also[0]}` : "connect";
    try {
      const { url } = await send<{ url: string }>(`${PATH}/connect`, { also });
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
      if (id === "calendar_write") await loadCalendars();
      if (id === "drive_files") await loadFiles();
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
        <h3 id="google-title" class="text-base font-semibold">Google Calendar, Gmail, Drive and Contacts</h3>
        <InfoGlyph label="About Google">
          Dom reads your calendar and mail, can save drafts and look up contacts, and works with Drive files, only in your DM. In Drive he
          sees only files he made and files you hand him. Every change to a calendar, doc or sheet and every draft asks you first, and Dom
          never sends mail. Each switch is off until you turn it on.
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
            <span class="flex shrink-0 items-center gap-2">
              {#if item.extra && !item.granted}
                <Button type="button" variant="outline" size="sm" disabled={busy !== ""} onclick={() => connect([item.id])}>
                  {#if busy === `connect-${item.id}`}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Opening Google…{:else}Reconnect to allow{/if}
                </Button>
              {/if}
              <Switch
                id={`google-${item.id}`}
                checked={item.on}
                disabled={!item.granted || busy !== ""}
                onCheckedChange={(on) => toggle(item.id, on)}
              />
            </span>
          </li>
        {/each}
      </ul>
      {#if !view.own_calendars_granted}
        <p class="text-sm text-muted-foreground">Reconnect to let Dom keep his own calendars. Everything else keeps working until then.</p>
      {:else}
        <div class="grid gap-1.5" aria-labelledby="google-calendars-title">
          <h4 id="google-calendars-title" class="text-sm font-medium">Dom's calendars</h4>
          {#if calendars === null}
            <p class="text-sm text-muted-foreground">Asking Google…</p>
          {:else if calendars.length === 0}
            <p class="text-sm text-muted-foreground">None yet. Dom asks you before he makes one.</p>
          {:else}
            <ul class="grid gap-1.5">
              {#each calendars as item (item.id)}
                <li class="flex items-center gap-2 text-sm">
                  <span
                    class="size-3 shrink-0 rounded-full border bg-raised"
                    style:background-color={item.color || undefined}
                    aria-hidden="true"
                  ></span>
                  <span class="min-w-0 flex-1 break-words">{item.name}</span>
                  <span class="shrink-0 text-xs text-muted-foreground">
                    {#if item.upcoming === null}events unknown{:else}{item.upcoming}{item.more ? "+" : ""} upcoming{/if}
                  </span>
                  <IconButton label={`Remove ${item.name}`} icon={Trash2} disabled={busy !== ""} onclick={() => (removing = item)} />
                </li>
              {/each}
            </ul>
          {/if}
        </div>
      {/if}
      {#if view.switches.find((item) => item.id === "drive_files")?.granted}
        <div class="grid gap-1.5" aria-labelledby="google-files-title">
          <h4 id="google-files-title" class="text-sm font-medium">Dom's files</h4>
          {#if files === null}
            <p class="text-sm text-muted-foreground">Asking Google…</p>
          {:else}
            <p class="text-sm text-muted-foreground">
              Dom sees only files he made in his own folder and files you hand him.
              {#if files.folder}<a class="underline" href={files.folder.url} target="_blank" rel="noreferrer">Open Dom's folder</a>{/if}
            </p>
            {#if files.files.length === 0}
              <p class="text-sm text-muted-foreground">None yet.</p>
            {:else}
              <ul class="grid gap-1">
                {#each files.files as item, index (index)}
                  <li class="flex items-center gap-2 text-sm">
                    <span class="min-w-0 flex-1 break-words">{item.name}</span>
                    <span class="shrink-0 text-xs text-muted-foreground">{item.kind}</span>
                  </li>
                {/each}
              </ul>
              {#if files.more}<p class="text-xs text-muted-foreground">More files exist than are listed.</p>{/if}
            {/if}
          {/if}
          {#if view.picker_ready}
            <p class="text-sm"><a class="underline" href={view.picker_path}>Hand Dom a file</a> with Google's file picker. Or send him the file's link in your DM.</p>
          {:else}
            <p class="text-sm text-muted-foreground">To hand Dom files, add GOOGLE_PICKER_API_KEY and GOOGLE_PROJECT_NUMBER to .env and restart (docs/google.md).</p>
          {/if}
        </div>
      {/if}
      {#if view.own_calendars_granted}
        <BookingWindows />
      {/if}
      {#if view.switches.some((item) => !item.granted && !item.extra)}
        <p class="text-xs text-muted-foreground">A switch is locked when you did not tick that permission on Google's screen. Connect again to grant it.</p>
      {/if}
    {/if}

    <div class="flex flex-wrap items-center gap-2">
      <Button
        type="button"
        size="sm"
        onclick={() => connect()}
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

<AlertDialog.Root open={removing !== null} onOpenChange={(open) => !open && (removing = null)}>
  <AlertDialog.Content>
    {#if removing}
      <AlertDialog.Header>
        <AlertDialog.Title>Remove {removing.name}?</AlertDialog.Title>
        <AlertDialog.Description>
          This deletes the calendar and every event on it from your Google account, and cancels Dom's nudges for them. It can't be undone.
        </AlertDialog.Description>
      </AlertDialog.Header>
      <AlertDialog.Footer>
        <AlertDialog.Cancel variant="outline">Keep it</AlertDialog.Cancel>
        <AlertDialog.Action variant="destructive-solid" onclick={() => removing && removeCalendar(removing)}>Remove</AlertDialog.Action>
      </AlertDialog.Footer>
    {/if}
  </AlertDialog.Content>
</AlertDialog.Root>
