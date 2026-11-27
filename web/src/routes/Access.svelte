<script lang="ts">
  import Eye from "@lucide/svelte/icons/eye";
  import EyeOff from "@lucide/svelte/icons/eye-off";
  import KeyRound from "@lucide/svelte/icons/key-round";
  import Pencil from "@lucide/svelte/icons/pencil";
  import Plus from "@lucide/svelte/icons/plus";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import Trash from "@lucide/svelte/icons/trash";
  import { onMount } from "svelte";
  import {
    getJSON,
    send,
    type AccessData,
    type DesktopAction,
    type PermissionPolicy,
    type SavedLogin,
    type SiteRule,
  } from "$lib/api";
  import IconButton from "$lib/components/kit/IconButton.svelte";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import PageTabs from "$lib/components/PageTabs.svelte";
  import SectionTabs from "$lib/components/kit/SectionTabs.svelte";
  import StatusNote from "$lib/components/StatusNote.svelte";
  import Confirm from "$lib/components/work/Confirm.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import * as Card from "$lib/components/ui/card";
  import { Checkbox } from "$lib/components/ui/checkbox";
  import { Input } from "$lib/components/ui/input";
  import { Label } from "$lib/components/ui/label";
  import * as Tabs from "$lib/components/ui/tabs";
  import type { Destination, Tab } from "$lib/nav";
  import { toast } from "$lib/toast.svelte";

  let { destination, tab }: { destination: Destination; tab: Tab } = $props();

  let data = $state<AccessData | null>(null);
  let failed = $state("");
  let busy = $state(false);
  let section = $state("logins");

  // Saved login editor state (passwords are strictly write-only and cleared immediately after save).
  let editorOpen = $state(false);
  let editingId = $state<number | null>(null);
  let formSite = $state("");
  let formLabel = $state("");
  let formUsername = $state("");
  let formPassword = $state("");
  let formSubdomains = $state(false);
  let showPassword = $state(false);

  // Site rule form state.
  let ruleSite = $state("");
  let ruleAction = $state<SiteRule["action"]>("fill_login");
  let rulePolicy = $state<PermissionPolicy>("allow");

  // History filters.
  let filterAction = $state("");
  let filterStatus = $state("");

  // Confirm dialogs.
  let deletingLogin = $state<SavedLogin | null>(null);
  let deletingSecret = $state<{ key: string; label: string } | null>(null);
  let confirmingRotate = $state(false);

  const POLICY_OPTIONS: { value: PermissionPolicy; label: string }[] = [
    { value: "ask", label: "Ask" },
    { value: "allow", label: "Allow" },
    { value: "never", label: "Never" },
  ];
  const SITE_ACTIONS: { value: SiteRule["action"]; label: string }[] = [
    { value: "visit_site", label: "Visit unfamiliar sites" },
    { value: "fill_login", label: "Fill saved logins" },
    { value: "download", label: "Download files" },
    { value: "upload", label: "Upload files" },
  ];
  const STATUS_LABELS: Record<string, string> = {
    pending: "Pending",
    allowed_once: "Allowed once",
    allowed_always: "Allowed always",
    auto_allowed: "Allowed by rule",
    rejected: "Rejected",
    blocked: "Blocked",
    expired: "Expired",
  };
  const selectClass =
    "h-10 rounded-lg border border-input bg-transparent px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50";

  async function load(query = "") {
    try {
      data = await getJSON<AccessData>(`/api/access${query}`);
      failed = "";
    } catch (error) {
      failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  onMount(() => load());

  async function post<T extends AccessData & { message?: string }>(path: string, body: unknown, fallbackMsg = "Saved.") {
    busy = true;
    try {
      const result = await send<T>(path, body);
      data = result;
      toast.show("ok", result.message || fallbackMsg);
      return true;
    } catch {
      return false;
    } finally {
      busy = false;
    }
  }

  function openAddLogin() {
    editingId = null;
    formSite = "";
    formLabel = "";
    formUsername = "";
    formPassword = "";
    formSubdomains = false;
    showPassword = false;
    editorOpen = true;
  }

  function openEditLogin(login: SavedLogin) {
    editingId = login.id;
    formSite = login.site;
    formLabel = login.label;
    formUsername = "";
    formPassword = "";
    formSubdomains = login.match_subdomains;
    showPassword = false;
    editorOpen = true;
  }

  function closeEditor() {
    editorOpen = false;
    formPassword = "";
    showPassword = false;
  }

  async function submitLogin(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;
    const payload: Record<string, unknown> = {
      site: formSite.trim(),
      label: formLabel.trim(),
      username: formUsername.trim(),
      password: formPassword,
      match_subdomains: formSubdomains,
    };
    if (editingId !== null) payload.id = editingId;
    const ok = await post<AccessData & { message: string }>("/api/access/logins/save", payload, "Login saved.");
    if (ok) closeEditor();
  }

  async function submitSiteRule(event: SubmitEvent) {
    event.preventDefault();
    if (busy || !ruleSite.trim()) return;
    const ok = await post<AccessData & { message: string }>("/api/access/site-rule", {
      site: ruleSite.trim(),
      action: ruleAction,
      policy: rulePolicy,
    });
    if (ok) ruleSite = "";
  }

  async function reloadHistory() {
    const params = new URLSearchParams();
    if (filterAction) params.set("action", filterAction);
    if (filterStatus) params.set("status", filterStatus);
    const qs = params.toString();
    await load(qs ? `?${qs}` : "");
  }

  function statusBadgeVariant(status: string): "secondary" | "destructive" | "outline" {
    if (status === "allowed_once" || status === "allowed_always" || status === "auto_allowed") return "secondary";
    if (status === "rejected" || status === "blocked") return "destructive";
    return "outline";
  }

  function shortTime(iso: string | null): string {
    if (!iso) return "";
    const parsed = new Date(iso);
    return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }
</script>

<PageHeader kicker={destination.label} title={tab.label} subtitle={tab.blurb} />
<PageTabs {destination} {tab} />

{#if data === null && failed}
  <StatusNote tone="error" title="Couldn't load access settings" detail={`Dom's dashboard answered: ${failed}.`}>
    <Button variant="outline" onclick={() => load()}>Try again</Button>
  </StatusNote>
{:else if data === null}
  <p class="sr-only" role="status">Loading…</p>
  <div class="h-44 animate-pulse rounded-xl border bg-surface" aria-hidden="true"></div>
{:else}
  {#if data.pending_permissions.length > 0}
    <Card.Root class="mb-5 border-signal/50">
      <Card.Header>
        <Card.Title class="text-base">Pending approvals ({data.pending_permissions.length})</Card.Title>
        <Card.Description>Dom is waiting for your decision: a desktop task, or a Google Calendar or Gmail change.</Card.Description>
      </Card.Header>
      <Card.Content class="grid gap-3">
        {#each data.pending_permissions as req (req.id)}
          <div class="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-raised/40 p-3">
            <div class="min-w-0 flex-1">
              <p class="text-sm font-medium">{req.action_label}{#if req.site} · <span class="font-mono">{req.site}</span>{/if}</p>
              {#if req.detail}<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div role="region" aria-label={`Details of ${req.action_label}`} tabindex="0" class="max-h-48 overflow-y-auto outline-none focus-visible:ring-3 focus-visible:ring-ring/50 text-xs break-words whitespace-pre-line text-muted-foreground">{req.detail}</div>{/if}
              {#if req.dashboard_only}<p class="text-xs text-coral">Too long for the Telegram card: approve it here.</p>{/if}
            </div>
            <div class="flex flex-wrap gap-2">
              <Button size="sm" disabled={busy} onclick={() => post("/api/access/decide", { id: req.id, decision: "once" })}>Allow once</Button>
              {#if req.can_allow_always !== false}
                <Button size="sm" variant="secondary" disabled={busy} onclick={() => post("/api/access/decide", { id: req.id, decision: "always" })}>Allow always</Button>
              {/if}
              <Button size="sm" variant="destructive" disabled={busy} onclick={() => post("/api/access/decide", { id: req.id, decision: "reject" })}>Reject</Button>
            </div>
          </div>
        {/each}
      </Card.Content>
    </Card.Root>
  {/if}

  <Tabs.Root bind:value={section}>
    <SectionTabs
      items={[
        { value: "logins", label: "Saved logins" },
        { value: "permissions", label: "Permissions" },
        { value: "history", label: "Approvals history" },
      ]}
    />

    <!-- Tab 1: Saved logins, owner secrets, and encryption key -->
    <Tabs.Content value="logins" class="grid gap-5">
      <Card.Root>
        <Card.Header class="flex flex-row flex-wrap items-start justify-between gap-3">
          <div class="grid gap-1">
            <Card.Title class="flex items-center gap-1.5 text-base">
              Saved desktop logins
              <InfoGlyph label="How saved logins stay private">
                Encrypted at rest with AES-256-GCM using DOM_MASTER_KEY in .env. When approved, Dom fills them directly
                over VNC keystrokes without the password ever passing through a model, sandbox HTTP trace, or log.
              </InfoGlyph>
            </Card.Title>
            <Card.Description>Passwords are write-only and never shown again once saved.</Card.Description>
          </div>
          <Button size="sm" disabled={busy} onclick={openAddLogin}>
            <Plus class="size-4" aria-hidden="true" />
            Add login
          </Button>
        </Card.Header>
        <Card.Content class="grid gap-4">
          {#if editorOpen}
            <form class="grid gap-3 rounded-xl border bg-raised/40 p-4" onsubmit={submitLogin}>
              <div class="flex items-center justify-between gap-2">
                <h3 class="text-sm font-medium">{editingId === null ? "Add saved login" : "Edit saved login"}</h3>
                <Button type="button" variant="ghost" size="sm" onclick={closeEditor}>Cancel</Button>
              </div>
              <div class="grid gap-3 sm:grid-cols-2">
                <div class="grid gap-1.5">
                  <Label for="login-site">Site hostname</Label>
                  <Input id="login-site" bind:value={formSite} placeholder="example.org" required autocomplete="off" />
                </div>
                <div class="grid gap-1.5">
                  <Label for="login-label">Label (optional)</Label>
                  <Input id="login-label" bind:value={formLabel} placeholder="Personal account" autocomplete="off" />
                </div>
                <div class="grid gap-1.5">
                  <Label for="login-username">{editingId === null ? "Username or email" : "New username (leave blank to keep)"}</Label>
                  <Input id="login-username" bind:value={formUsername} placeholder={editingId === null ? "you@example.org" : "Leave blank to keep existing"} autocomplete="off" spellcheck={false} />
                </div>
                <div class="grid gap-1.5">
                  <Label for="login-password">{editingId === null ? "Password" : "New password (leave blank to keep)"}</Label>
                  <div class="flex items-center gap-1.5">
                    <Input
                      id="login-password"
                      type={showPassword ? "text" : "password"}
                      bind:value={formPassword}
                      required={editingId === null}
                      autocomplete="off"
                      spellcheck={false}
                      class="flex-1"
                    />
                    <IconButton
                      label={showPassword ? "Hide password" : "Show password"}
                      icon={showPassword ? EyeOff : Eye}
                      variant="outline"
                      type="button"
                      onclick={() => (showPassword = !showPassword)}
                    />
                  </div>
                </div>
              </div>
              <div class="flex items-center gap-2">
                <Checkbox id="login-subdomains" bind:checked={formSubdomains} />
                <Label for="login-subdomains" class="text-sm font-normal">Also match subdomains of this site (HTTPS only)</Label>
              </div>
              <div class="flex flex-wrap gap-2">
                <Button type="submit" disabled={busy}>{editingId === null ? "Save login" : "Update login"}</Button>
                <Button type="button" variant="outline" disabled={busy} onclick={closeEditor}>Cancel</Button>
              </div>
            </form>
          {/if}

          {#if data.logins.length === 0}
            <p class="text-sm text-muted-foreground">No saved logins yet. Add one here, or let Dom ask via a secure request card.</p>
          {:else}
            <ul class="grid gap-2" aria-label="Saved logins">
              {#each data.logins as login (login.id)}
                <li class="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-surface px-3.5 py-2.5">
                  <div class="min-w-0 flex-1">
                    <p class="flex flex-wrap items-center gap-2 text-sm font-medium">
                      <span class="font-mono">{login.site}</span>
                      {#if login.label && login.label !== login.site}<span class="text-muted-foreground">{login.label}</span>{/if}
                      {#if login.match_subdomains}<Badge variant="outline">subdomains</Badge>{/if}
                    </p>
                    <p class="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                      <span>User: <span class="font-mono">{login.username_hint || "—"}</span></span>
                      <span>Password: <span class="font-mono">••••••••</span></span>
                      <span>Updated {shortTime(login.updated_at)}</span>
                    </p>
                  </div>
                  <div class="flex items-center gap-1">
                    <IconButton label={`Edit login for ${login.site}`} icon={Pencil} disabled={busy} onclick={() => openEditLogin(login)} />
                    <IconButton label={`Delete login for ${login.site}`} icon={Trash} disabled={busy} onclick={() => (deletingLogin = login)} />
                  </div>
                </li>
              {/each}
            </ul>
          {/if}
        </Card.Content>
      </Card.Root>

      <Card.Root>
        <Card.Header>
          <Card.Title class="flex items-center gap-1.5 text-base">
            Stored secrets
            <InfoGlyph label="About stored secrets">
              Secrets you entered through Dom's one-time secure request cards. Values stay encrypted and are never shown again.
            </InfoGlyph>
          </Card.Title>
          <Card.Description>Dom only knows these secrets are set; their values never appear in chat or traces.</Card.Description>
        </Card.Header>
        <Card.Content>
          {#if data.owner_secrets.length === 0}
            <p class="text-sm text-muted-foreground">No stored secrets yet.</p>
          {:else}
            <ul class="grid gap-2" aria-label="Stored secrets">
              {#each data.owner_secrets as item (item.key)}
                <li class="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-surface px-3.5 py-2.5">
                  <div class="min-w-0 flex-1">
                    <p class="text-sm font-medium">{item.label} <span class="font-mono text-xs text-muted-foreground">({item.key})</span></p>
                    <p class="text-xs text-muted-foreground">Value: <span class="font-mono">••••••••</span> · Updated {shortTime(item.updated_at)}</p>
                  </div>
                  <IconButton
                    label={`Delete secret ${item.label}`}
                    icon={Trash}
                    disabled={busy}
                    onclick={() => (deletingSecret = { key: item.key, label: item.label })}
                  />
                </li>
              {/each}
            </ul>
          {/if}
        </Card.Content>
      </Card.Root>

      <Card.Root>
        <Card.Header class="flex flex-row flex-wrap items-start justify-between gap-3">
          <div class="grid gap-1">
            <Card.Title class="flex items-center gap-1.5 text-base">
              <KeyRound class="size-4 text-muted-foreground" aria-hidden="true" />
              Encryption key
              <InfoGlyph label="How DOM_MASTER_KEY works">
                Stored in DOM_HOME/.env (mode 0600). Rotating generates a new 256-bit key and re-encrypts every saved
                login, secret, and OAuth token in one atomic transaction.
              </InfoGlyph>
            </Card.Title>
            <Card.Description>
              {#if data.key_status.error}
                {data.key_status.error}
              {:else if data.key_status.configured}
                Active key <code class="font-mono">{data.key_status.key_id}</code> · {data.key_status.total_entries} encrypted {data.key_status.total_entries === 1 ? "entry" : "entries"}.
              {:else}
                A fresh 256-bit key will be generated in .env automatically when you save your first secret.
              {/if}
            </Card.Description>
          </div>
          <Button variant="outline" size="sm" disabled={busy} onclick={() => (confirmingRotate = true)}>
            <RefreshCw class="size-4" aria-hidden="true" />
            Rotate key
          </Button>
        </Card.Header>
      </Card.Root>
    </Tabs.Content>

    <!-- Tab 2: Desktop permissions & per-site rules -->
    <Tabs.Content value="permissions" class="grid gap-5">
      <Card.Root>
        <Card.Header>
          <Card.Title class="text-base">Default action policies</Card.Title>
          <Card.Description>Choose Ask, Allow, or Never for each action, including each kind of Google Calendar and Gmail change. Per-site rules below override these defaults.</Card.Description>
        </Card.Header>
        <Card.Content class="grid gap-3">
          {#each data.actions as item (item.id)}
            <div class="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-surface px-3.5 py-3">
              <div class="min-w-0 flex-1">
                <p class="flex items-center gap-1.5 text-sm font-medium">
                  {item.label}
                  <InfoGlyph label={`About ${item.label}`}>{item.description}</InfoGlyph>
                </p>
                {#if item.policy === "never" && item.site_allow_count > 0}
                  <p class="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    <span>{item.site_allow_count} site {item.site_allow_count === 1 ? "rule still overrides" : "rules still override"} this with Allow.</span>
                    <button
                      type="button"
                      class="underline hover:text-foreground"
                      disabled={busy}
                      onclick={() => post("/api/access/site-rules/clear-allows", { action: item.id })}
                    >
                      Clear site allows
                    </button>
                  </p>
                {/if}
              </div>
              <div class="inline-flex rounded-lg border bg-raised/40 p-0.5" role="group" aria-label={`Policy for ${item.label}`}>
                {#each POLICY_OPTIONS as opt (opt.value)}
                  <button
                    type="button"
                    disabled={busy || (item.asks_every_time && opt.value === "allow")}
                    aria-pressed={item.policy === opt.value}
                    class={item.policy === opt.value
                      ? "rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
                      : "rounded-md px-3 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground disabled:opacity-40"}
                    onclick={() => item.policy !== opt.value && post("/api/access/policy", { action: item.id as DesktopAction, policy: opt.value })}
                  >
                    {opt.label}
                  </button>
                {/each}
              </div>
            </div>
          {/each}
        </Card.Content>
      </Card.Root>

      <Card.Root>
        <Card.Header>
          <Card.Title class="text-base">Per-site rules</Card.Title>
          <Card.Description>Sites you tapped Allow always on appear here, and you can add or revoke site rules at any time.</Card.Description>
        </Card.Header>
        <Card.Content class="grid gap-4">
          <form class="flex flex-wrap items-end gap-3" onsubmit={submitSiteRule}>
            <div class="grid min-w-44 flex-1 gap-1.5">
              <Label for="rule-site">Site hostname</Label>
              <Input id="rule-site" bind:value={ruleSite} placeholder="example.org" required autocomplete="off" />
            </div>
            <div class="grid gap-1.5">
              <Label for="rule-action">Action</Label>
              <select id="rule-action" class={selectClass} bind:value={ruleAction}>
                {#each SITE_ACTIONS as item (item.value)}<option value={item.value}>{item.label}</option>{/each}
              </select>
            </div>
            <div class="grid gap-1.5">
              <Label for="rule-policy">Rule</Label>
              <select id="rule-policy" class={selectClass} bind:value={rulePolicy}>
                {#each POLICY_OPTIONS as opt (opt.value)}<option value={opt.value}>{opt.label}</option>{/each}
              </select>
            </div>
            <Button type="submit" disabled={busy}>Add rule</Button>
          </form>

          {#if data.site_rules.length === 0}
            <p class="text-sm text-muted-foreground">No per-site rules yet. Every site follows the defaults above.</p>
          {:else}
            <ul class="grid gap-2" aria-label="Per-site permissions">
              {#each data.site_rules as rule (`${rule.site}:${rule.action}`)}
                <li class="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-surface px-3.5 py-2.5">
                  <div class="min-w-0 flex-1">
                    <p class="flex flex-wrap items-center gap-2 text-sm font-medium">
                      <span class="font-mono">{rule.site}</span>
                      <Badge variant="outline">{SITE_ACTIONS.find((a) => a.value === rule.action)?.label ?? rule.action}</Badge>
                      <Badge variant={rule.policy === "allow" ? "secondary" : rule.policy === "never" ? "destructive" : "outline"}>{rule.policy}</Badge>
                    </p>
                  </div>
                  <div class="flex items-center gap-1.5">
                    <div class="inline-flex rounded-lg border bg-raised/40 p-0.5" role="group" aria-label={`Rule for ${rule.site} ${rule.action}`}>
                      {#each POLICY_OPTIONS as opt (opt.value)}
                        <button
                          type="button"
                          disabled={busy}
                          aria-pressed={rule.policy === opt.value}
                          class={rule.policy === opt.value
                            ? "rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground"
                            : "rounded-md px-2.5 py-1 text-xs font-medium text-muted-foreground hover:text-foreground"}
                          onclick={() => rule.policy !== opt.value && post("/api/access/site-rule", { site: rule.site, action: rule.action, policy: opt.value })}
                        >
                          {opt.label}
                        </button>
                      {/each}
                    </div>
                    <IconButton
                      label={`Remove rule for ${rule.site}`}
                      icon={Trash}
                      disabled={busy}
                      onclick={() => post("/api/access/site-rule", { site: rule.site, action: rule.action, policy: null })}
                    />
                  </div>
                </li>
              {/each}
            </ul>
          {/if}
        </Card.Content>
      </Card.Root>
    </Tabs.Content>

    <!-- Tab 3: Approvals history -->
    <Tabs.Content value="history" class="grid gap-4">
      <Card.Root>
        <Card.Header>
          <Card.Title class="text-base">Approvals history</Card.Title>
          <Card.Description>Newest-first audit log of desktop and Google approval checks and owner decisions.</Card.Description>
        </Card.Header>
        <Card.Content class="grid gap-4">
          <div class="flex flex-wrap gap-3" role="group" aria-label="Filter approvals history">
            <div class="grid gap-1">
              <Label for="hist-action">Action</Label>
              <select id="hist-action" class={selectClass} bind:value={filterAction} onchange={reloadHistory}>
                <option value="">All actions</option>
                {#each data.actions as item (item.id)}<option value={item.id}>{item.label}</option>{/each}
              </select>
            </div>
            <div class="grid gap-1">
              <Label for="hist-status">Outcome</Label>
              <select id="hist-status" class={selectClass} bind:value={filterStatus} onchange={reloadHistory}>
                <option value="">All outcomes</option>
                {#each Object.entries(STATUS_LABELS) as [key, label] (key)}<option value={key}>{label}</option>{/each}
              </select>
            </div>
          </div>

          {#if data.history.length === 0}
            <p class="text-sm text-muted-foreground">No permission events recorded yet.</p>
          {:else}
            <ul class="grid gap-2" aria-label="Approvals history">
              {#each data.history as row (row.id)}
                <li class="flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-surface px-3.5 py-2.5 text-sm">
                  <div class="min-w-0 flex-1">
                    <p class="flex flex-wrap items-center gap-2 font-medium">
                      <span>{row.action_label}</span>
                      {#if row.site}<span class="font-mono text-xs text-muted-foreground">{row.site}</span>{/if}
                      <Badge variant={statusBadgeVariant(row.status)}>{STATUS_LABELS[row.status] ?? row.status}</Badge>
                    </p>
                    {#if row.detail}<p class="mt-0.5 text-xs break-words text-muted-foreground">{row.detail}</p>{/if}
                  </div>
                  <div class="text-right text-xs text-muted-foreground">
                    <p>{shortTime(row.decided_at ?? row.created_at)}</p>
                    {#if row.decided_via}<p>via {row.decided_via}</p>{/if}
                  </div>
                </li>
              {/each}
            </ul>
          {/if}
        </Card.Content>
      </Card.Root>
    </Tabs.Content>
  </Tabs.Root>
{/if}

<Confirm
  bind:open={() => deletingLogin !== null, (open) => !open && (deletingLogin = null)}
  title={`Delete saved login for ${deletingLogin?.site}?`}
  description="Its encrypted username and password are permanently removed."
  keep="Keep it"
  action="Delete login"
  onconfirm={() => deletingLogin && post("/api/access/logins/delete", { id: deletingLogin.id }).then(() => (deletingLogin = null))}
/>
<Confirm
  bind:open={() => deletingSecret !== null, (open) => !open && (deletingSecret = null)}
  title={`Delete stored secret "${deletingSecret?.label}"?`}
  description="Its encrypted value is permanently removed."
  keep="Keep it"
  action="Delete secret"
  onconfirm={() => deletingSecret && post("/api/access/secrets/delete", { key: deletingSecret.key }).then(() => (deletingSecret = null))}
/>
<Confirm
  bind:open={confirmingRotate}
  title="Rotate DOM_MASTER_KEY?"
  description="Dom generates a new 256-bit key in .env and re-encrypts every saved login, secret, and OAuth token in one atomic step."
  keep="Cancel"
  action="Rotate key"
  onconfirm={() => post("/api/access/rotate-key", {}).then(() => (confirmingRotate = false))}
/>
