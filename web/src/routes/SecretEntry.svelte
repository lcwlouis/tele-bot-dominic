<script lang="ts">
  import Eye from "@lucide/svelte/icons/eye";
  import EyeOff from "@lucide/svelte/icons/eye-off";
  import ShieldCheck from "@lucide/svelte/icons/shield-check";
  import { onMount } from "svelte";
  import { send, type SecretRequestLookup } from "$lib/api";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Input } from "$lib/components/ui/input";
  import { Label } from "$lib/components/ui/label";
  import { toast } from "$lib/toast.svelte";

  // E101 one-time secure secret entry form. The token arrives in the URL fragment (#t=...) so it never hits HTTP
  // access logs, is immediately stripped from browser history, and is submitted only over same-origin JSON POST.
  let token = $state<string | null>(null);
  let requestId = $state<number | null>(null);
  let request = $state<SecretRequestLookup | null>(null);
  let loadError = $state("");
  let loading = $state(true);
  let busy = $state(false);
  let done = $state<"saved" | "cancelled" | null>(null);

  let secretValue = $state("");
  let loginSite = $state("");
  let loginUsername = $state("");
  let loginPassword = $state("");
  let reveal = $state(false);

  onMount(async () => {
    const hash = window.location.hash.replace(/^#/, "");
    const hashParams = new URLSearchParams(hash);
    const searchParams = new URLSearchParams(window.location.search);
    // Only accept the one-time token from the URL fragment (#t=...), never a query parameter (?t=...) which would
    // hit HTTP access logs. Scrub any token from the address bar immediately.
    const rawToken = hashParams.get("t");
    const rawId = searchParams.get("id");
    if (rawToken || searchParams.has("t")) {
      window.history.replaceState(null, "", "/app/secret");
    }
    if (rawToken) {
      token = rawToken;
    } else if (rawId && /^\d+$/.test(rawId)) {
      requestId = Number(rawId);
    } else {
      loading = false;
      loadError =
        "No secret request token was found. If Cloudflare Access asked you to sign in, the one-time link fragment " +
        "(#t=...) is dropped during sign-in — open Waiting for you to complete the pending secret request.";
      return;
    }

    try {
      const payload = token ? { token } : { request_id: requestId };
      const res = await send<{ ok: boolean; request: SecretRequestLookup }>("/api/secrets/request/lookup", payload);
      request = res.request;
      if (res.request.target === "login") {
        loginSite = res.request.target_key;
      }
    } catch (error) {
      loadError = error instanceof Error ? error.message : "This secret request has expired or was already used.";
    } finally {
      loading = false;
    }
  });

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (!request || busy) return;
    busy = true;
    try {
      const base = token ? { token } : { request_id: request.id };
      const body =
        request.target === "login"
          ? { ...base, site: loginSite.trim(), username: loginUsername.trim(), password: loginPassword }
          : { ...base, value: secretValue };
      await send<{ ok: boolean }>("/api/secrets/request/submit", body);
      secretValue = "";
      loginPassword = "";
      reveal = false;
      done = "saved";
      toast.show("ok", "Secret saved securely.");
    } catch {
      // toast already shown by send()
    } finally {
      busy = false;
    }
  }

  async function cancel() {
    if (!request || busy) return;
    busy = true;
    try {
      const base = token ? { token } : { request_id: request.id };
      await send<{ ok: boolean }>("/api/secrets/request/cancel", base);
      secretValue = "";
      loginPassword = "";
      done = "cancelled";
      toast.show("ok", "Secret request cancelled.");
    } catch {
      // toast already shown by send()
    } finally {
      busy = false;
    }
  }

  function destinationLabel(item: SecretRequestLookup): string {
    if (item.target === "env") return `.env variable ${item.target_key}`;
    if (item.target === "login") return item.target_key ? `Saved login for ${item.target_key}` : "Saved desktop login";
    return `Encrypted credential store (${item.target_key})`;
  }
</script>

<PageHeader kicker="Secure entry" title="Enter secret" subtitle="Your value goes straight to Dom's encrypted store or .env — never through chat or a model." />

{#if loading}
  <p class="sr-only" role="status">Loading secret request…</p>
  <div class="h-40 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else if done === "saved"}
  <StatusNote title="Secret saved securely" detail="Dom was notified only that it is set. You can close this page or return to the dashboard.">
    <Button href="/app/system/access" variant="outline">View Access</Button>
  </StatusNote>
{:else if done === "cancelled"}
  <StatusNote title="Secret request cancelled" detail="Nothing was saved, and Dom was notified that you cancelled the request.">
    <Button href="/app/waiting" variant="outline">Back to Waiting</Button>
  </StatusNote>
{:else if loadError || !request}
  <StatusNote tone="error" title="Request unavailable" detail={loadError || "This secret request has expired or was already used."}>
    <Button href="/app/waiting" variant="outline">Open Waiting for you</Button>
  </StatusNote>
{:else}
  <Card.Root class="max-w-xl">
    <Card.Header>
      <Card.Title class="flex items-center gap-2 text-base">
        <ShieldCheck class="size-4 text-signal-strong" aria-hidden="true" />
        {request.label}
      </Card.Title>
      <Card.Description class="break-words">{request.reason}</Card.Description>
    </Card.Header>
    <Card.Content class="grid gap-4">
      <div class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge variant="outline">{destinationLabel(request)}</Badge>
        {#if request.overwrites_existing}
          <Badge variant="destructive">Overwrites existing value</Badge>
        {/if}
      </div>

      <form class="grid gap-4" onsubmit={submit}>
        {#if request.target === "login"}
          <div class="grid gap-1.5">
            <Label for="sreq-site">Site hostname</Label>
            <Input id="sreq-site" bind:value={loginSite} placeholder="example.org" required autocomplete="off" />
          </div>
          <div class="grid gap-1.5">
            <Label for="sreq-user">Username or email</Label>
            <Input id="sreq-user" bind:value={loginUsername} placeholder="you@example.org" autocomplete="off" spellcheck={false} />
          </div>
          <div class="grid gap-1.5">
            <Label for="sreq-pass">Password</Label>
            <div class="flex items-center gap-1.5">
              <Input
                id="sreq-pass"
                type={reveal ? "text" : "password"}
                bind:value={loginPassword}
                required
                autocomplete="off"
                spellcheck={false}
                class="flex-1"
              />
              <IconButton
                label={reveal ? "Hide password" : "Show password"}
                icon={reveal ? EyeOff : Eye}
                variant="outline"
                type="button"
                onclick={() => (reveal = !reveal)}
              />
            </div>
          </div>
        {:else}
          <div class="grid gap-1.5">
            <Label for="sreq-value">Secret value</Label>
            <div class="flex items-center gap-1.5">
              <Input
                id="sreq-value"
                type={reveal ? "text" : "password"}
                bind:value={secretValue}
                required
                autocomplete="off"
                spellcheck={false}
                placeholder="Paste secret value"
                class="flex-1"
              />
              <IconButton
                label={reveal ? "Hide secret" : "Show secret"}
                icon={reveal ? EyeOff : Eye}
                variant="outline"
                type="button"
                onclick={() => (reveal = !reveal)}
              />
            </div>
          </div>
        {/if}

        <div class="flex flex-wrap gap-2">
          <Button type="submit" disabled={busy}>Save securely</Button>
          <Button type="button" variant="destructive" disabled={busy} onclick={cancel}>Cancel request</Button>
        </div>
      </form>
    </Card.Content>
  </Card.Root>
{/if}
