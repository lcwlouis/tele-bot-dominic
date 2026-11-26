<script lang="ts" module>
  import CircleAlert from "@lucide/svelte/icons/circle-alert";
  import CircleCheck from "@lucide/svelte/icons/circle-check";
  import CircleMinus from "@lucide/svelte/icons/circle-minus";
  import CircleX from "@lucide/svelte/icons/circle-x";
  import type { ServiceState } from "$lib/api";

  /** Each service state as a glyph, a word and a tone: colour is never the only cue. */
  export const SERVICE_STATE: Record<ServiceState, { label: string; icon: typeof CircleCheck; tone: string }> = {
    ok: { label: "Working", icon: CircleCheck, tone: "text-status-done" },
    missing: { label: "Needs setup", icon: CircleAlert, tone: "text-status-waiting" },
    error: { label: "Error", icon: CircleX, tone: "text-status-failed" },
    off: { label: "Off", icon: CircleMinus, tone: "text-muted-foreground" },
  };
</script>

<script lang="ts">
  import Activity from "@lucide/svelte/icons/activity";
  import LoaderCircle from "@lucide/svelte/icons/loader-circle";
  import Self from "./ServiceCard.svelte";
  import { send, type Service, type ServicesView, type ServiceTestResult } from "$lib/api";
  import InfoGlyph from "$lib/components/kit/InfoGlyph.svelte";
  import { Badge } from "$lib/components/ui/badge";
  import { Button } from "$lib/components/ui/button";
  import { Input } from "$lib/components/ui/input";
  import { Switch } from "$lib/components/ui/switch";
  import { toast } from "$lib/toast.svelte";
  import { cn } from "$lib/utils";
  import { seconds } from "./roles";

  // One service (E81): its state, a line of detail, its fields and Test. A secret field is write-only: the page
  // only knows whether it is set, sends a new value when one is typed, and empties the box after every save.
  let {
    service,
    revision,
    onsaved,
    nested = false,
  }: { service: Service; revision: string; onsaved: (view: ServicesView) => void; nested?: boolean } = $props();

  type Value = string | number | boolean | null;
  let drafts = $state<Record<string, Value>>({});
  let clearing = $state<Record<string, boolean>>({}); // secrets to remove on the next save
  let saving = $state(false);
  let testing = $state(false);
  let result = $state<ServiceTestResult | null>(null);

  const id = $derived(`service-${service.id.replace(/[^a-z0-9_-]/gi, "-")}`);
  const look = $derived(SERVICE_STATE[service.state]);

  function reset() {
    drafts = {};
    clearing = {};
  }

  /** Only what changed: a secret when a new value is typed or it is to be removed; anything else when it differs. */
  const changes = $derived.by(() => {
    const out: Record<string, Value> = {};
    for (const field of service.fields) {
      if (!field.editable) continue;
      if (field.kind === "secret") {
        const typed = String(drafts[field.name] ?? "").trim();
        if (typed) out[field.name] = typed;
        else if (clearing[field.name]) out[field.name] = null;
      } else if (field.name in drafts && drafts[field.name] !== field.value) {
        out[field.name] = field.kind === "int" ? Number(drafts[field.name]) : drafts[field.name];
      }
    }
    return out;
  });
  const invalid = $derived(
    service.fields.some((field) => {
      if (field.kind !== "int" || !(field.name in drafts)) return false;
      const n = Number(drafts[field.name]);
      return !Number.isInteger(n) || (field.min !== null && n < field.min) || (field.max !== null && n > field.max);
    }),
  );

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (saving || invalid || Object.keys(changes).length === 0) return;
    saving = true;
    try {
      const view = await send<ServicesView>("/api/services/save", { revision, id: service.id, fields: changes });
      reset();
      result = null;
      onsaved(view);
      toast.show("ok", `${service.label} saved.`);
    } catch {
      // the toast says why; the typed values stay so they can be fixed
    } finally {
      saving = false;
    }
  }

  async function test() {
    testing = true;
    result = null;
    try {
      result = await send<ServiceTestResult>("/api/services/test", { revision, id: service.id });
    } catch {
      // the toast says why
    } finally {
      testing = false;
    }
  }

  const value = (name: string, saved: Value) => (name in drafts ? drafts[name] : saved);
</script>

<section
  aria-labelledby={`${id}-title`}
  class={cn("flex flex-col gap-3", nested ? "rounded-xl border bg-raised/30 p-3.5" : "")}
>
  <div class="flex flex-wrap items-start gap-x-3 gap-y-1">
    <span class={cn("mt-0.5 inline-flex items-center gap-1.5", look.tone)} data-state={service.state}>
      <look.icon class="size-4" aria-hidden="true" />
      <span class="sr-only">{look.label}:</span>
    </span>
    <div class="min-w-0 flex-1">
      <div class="flex flex-wrap items-center gap-1.5">
        <svelte:element this={nested ? "h4" : "h3"} id={`${id}-title`} class={cn("font-semibold break-all", nested ? "text-sm" : "text-base")}>
          {service.label}
        </svelte:element>
        {#if service.help}<InfoGlyph label={`About ${service.label}`}>{service.help}</InfoGlyph>{/if}
        <Badge variant="outline" class={look.tone}>{look.label}</Badge>
      </div>
      <p class={cn("mt-0.5 text-sm break-words", service.state === "error" || service.state === "missing" ? "text-foreground" : "text-muted-foreground")}>
        {service.detail}
      </p>
    </div>
  </div>

  {#if service.restart_required && !nested}
    <p class="text-sm text-coral">
      Saved, but Dom uses it only after a restart.
      <a href="#restart-dom" class="font-medium text-signal-strong underline underline-offset-2" onclick={(event) => {
        const button = document.getElementById("restart-dom");
        if (button) { event.preventDefault(); button.scrollIntoView({ block: "center" }); button.focus(); }
      }}>Restart Dom</a>
    </p>
  {/if}

  {#if service.permissions.length}
    <ul class="grid gap-1 text-sm">
      {#each service.permissions as permission (permission.id)}
        <li><span class="font-medium">{permission.label}:</span> <span class="text-muted-foreground">{permission.detail}</span></li>
      {/each}
    </ul>
  {/if}

  {#if service.links.length}
    <ul class="grid gap-1 text-sm">
      {#each service.links as link, index (index)}
        <li>{link.label}: <code class="rounded bg-raised px-1.5 py-0.5 font-mono text-xs break-all">{link.text}</code></li>
      {/each}
    </ul>
  {/if}

  {#if service.fields.length}
    <form onsubmit={save} class="grid gap-3">
      {#each service.fields as field (field.name)}
        {@const fid = `${id}-${field.name}`}
        {#if field.kind === "bool"}
          <div class="flex items-center justify-between gap-3">
            <span class="flex items-center gap-1">
              <label for={fid} class="text-sm">{field.label}</label>
              {#if field.help}<InfoGlyph label={`About ${field.label}`}>{field.help}</InfoGlyph>{/if}
            </span>
            <Switch
              id={fid}
              disabled={!field.editable || saving}
              bind:checked={() => Boolean(value(field.name, field.value)), (checked) => (drafts[field.name] = checked)}
            />
          </div>
        {:else}
          <div class="grid gap-1.5">
            <span class="flex items-center gap-1">
              <label for={fid} class="text-sm">{field.label}</label>
              {#if field.help}<InfoGlyph label={`About ${field.label}`}>{field.help}</InfoGlyph>{/if}
              {#if field.kind === "secret"}
                <Badge variant={field.is_set ? "secondary" : "outline"} class="ml-1" data-secret-state>{field.is_set ? "Set" : "Not set"}</Badge>
              {/if}
            </span>
            {#if field.kind === "secret"}
              <div class="flex flex-wrap items-center gap-2">
                <Input
                  id={fid}
                  type="password"
                  autocomplete="off"
                  spellcheck="false"
                  class="h-9 min-w-0 flex-1 basis-48 font-mono"
                  placeholder={field.is_set ? "Paste a new value to replace it" : "Paste the value"}
                  disabled={!field.editable || saving || clearing[field.name]}
                  bind:value={() => String(drafts[field.name] ?? ""), (text) => (drafts[field.name] = text)}
                />
                {#if field.is_set && field.editable}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-pressed={clearing[field.name] ?? false}
                    onclick={() => {
                      clearing[field.name] = !clearing[field.name];
                      drafts[field.name] = "";
                    }}
                  >
                    {clearing[field.name] ? "Keep it" : "Remove"} <span class="sr-only">{field.label}</span>
                  </Button>
                {/if}
              </div>
              {#if clearing[field.name]}<p class="text-xs text-coral">Saving removes it from .env.</p>{/if}
            {:else}
              <Input
                id={fid}
                type={field.kind === "int" ? "number" : field.kind === "url" ? "url" : "text"}
                inputmode={field.kind === "int" ? "numeric" : undefined}
                min={field.min ?? undefined}
                max={field.max ?? undefined}
                step={field.kind === "int" ? 1 : undefined}
                class={cn("h-9", field.kind === "int" ? "w-28" : "w-full", field.kind === "url" && "font-mono")}
                readonly={!field.editable}
                disabled={saving}
                aria-invalid={field.kind === "int" && field.name in drafts && invalid ? true : undefined}
                bind:value={() => value(field.name, field.value) ?? "", (text) => (drafts[field.name] = text)}
              />
            {/if}
          </div>
        {/if}
      {/each}
      {#if invalid}<p role="alert" class="text-sm text-coral">Enter a whole number in the allowed range.</p>{/if}
      <div class="flex flex-wrap items-center gap-2">
        <Button type="submit" size="sm" disabled={saving || invalid || Object.keys(changes).length === 0}>
          {#if saving}<LoaderCircle class="animate-spin motion-reduce:animate-none" aria-hidden="true" /> Saving…{:else}Save <span class="sr-only">{service.label}</span>{/if}
        </Button>
        {#if Object.keys(drafts).length || Object.keys(clearing).length}
          <Button type="button" variant="ghost" size="sm" onclick={reset} disabled={saving}>Undo changes</Button>
        {/if}
        {#if service.can_test}
          <Button type="button" variant="ghost" size="sm" onclick={test} disabled={testing}>
            <Activity data-icon="inline-start" aria-hidden="true" /> Test <span class="sr-only">{service.label}</span>
          </Button>
        {/if}
      </div>
    </form>
  {:else if service.can_test}
    <div>
      <Button type="button" variant="ghost" size="sm" onclick={test} disabled={testing}>
        <Activity data-icon="inline-start" aria-hidden="true" /> Test <span class="sr-only">{service.label}</span>
      </Button>
    </div>
  {/if}
  <p class="min-h-5 text-sm empty:min-h-0" aria-live="polite">
    {#if testing}
      <span class="text-muted-foreground">Testing…</span>
    {:else if result?.ok}
      <Badge>OK · {seconds(result.latency_ms)}</Badge> <span class="text-muted-foreground">{result.detail}</span>
    {:else if result}
      <span class="text-coral">{result.detail}</span>
    {/if}
  </p>

  {#each service.children as child (child.id)}
    <Self service={child} {revision} {onsaved} nested />
  {/each}
</section>
