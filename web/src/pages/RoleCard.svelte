<script lang="ts">
  import type { Endpoint, ModelRole, ModelTest, Provider, RoleSettings } from '../lib/api/types';
  import Badge from '../lib/components/Badge.svelte';
  import Button from '../lib/components/Button.svelte';
  import Card from '../lib/components/Card.svelte';
  import Icon from '../lib/components/Icon.svelte';
  import Select from '../lib/components/Select.svelte';
  import TextField from '../lib/components/TextField.svelte';
  import { toast } from '../lib/toast.svelte';

  interface Props {
    settings: RoleSettings;
    providers: Provider[];
    ontest: (role: ModelRole) => Promise<ModelTest>;
    onsave: (settings: RoleSettings) => Promise<void>;
  }

  let { settings, providers, ontest, onsave }: Props = $props();

  const ROLE: Record<ModelRole, { title: string; job: string }> = {
    chat: { title: 'Chat', job: "Writes Dom's replies." },
    fallback: { title: 'Fallback', job: 'Takes over the chat when the chat model and its backups are all down.' },
    media: { title: 'Photos and voice', job: 'Looks at images, GIFs and video, and listens to voice notes.' },
    drafting: { title: 'Drafting', job: 'Writes plugin and skill drafts for you to review.' },
  };

  const provider = (id: string) => providers.find((item) => item.id === id);
  const info = $derived(ROLE[settings.role]);
  const main = $derived(provider(settings.primary.provider));

  let testing = $state(false);
  let result = $state<ModelTest | null>(null);
  let editing = $state(false);
  let saving = $state(false);
  let draft = $state<Endpoint>({ provider: '', base_url: '', model: '' });

  const urlError = $derived(/^https?:\/\/\S+$/.test(draft.base_url) ? '' : 'Start with http:// or https://.');
  const modelError = $derived(draft.model.trim() ? '' : 'Name the model, as the provider spells it.');

  async function test() {
    testing = true;
    result = null;
    try {
      result = await ontest(settings.role);
    } catch (error) {
      result = { ok: false, latency_ms: null, message: error instanceof Error ? error.message : 'No answer' };
    } finally {
      testing = false;
    }
  }

  function edit() {
    draft = { ...settings.primary };
    editing = true;
  }

  function pickProvider(id: string) {
    const picked = provider(id);
    if (picked) draft.base_url = picked.base_url;
  }

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (urlError || modelError) return;
    saving = true;
    try {
      await onsave({ ...settings, primary: { ...draft, model: draft.model.trim() } });
      editing = false;
      result = null;
      toast(`${info.title} model saved`);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Not saved. Try again.', 'error');
    } finally {
      saving = false;
    }
  }
</script>

{#snippet key(item: Provider | undefined)}
  {#if !item}
    <Badge tone="danger">Unknown provider</Badge>
  {:else if item.api_key_env === null}
    <Badge>No key needed</Badge>
  {:else if item.key_set}
    <Badge tone="live">Key set</Badge>
  {:else}
    <Badge tone="danger">Key missing</Badge>
  {/if}
{/snippet}

<Card title={info.title}>
  {#snippet action()}
    {#if !editing}<Button variant="quiet" size="sm" onclick={edit}>Edit<span class="sr-only"> {info.title} model</span></Button>{/if}
  {/snippet}

  <p class="job">{info.job}</p>

  {#if editing}
    <form class="edit" onsubmit={save} novalidate>
      <Select
        label="Provider"
        options={providers.map((item) => ({ value: item.id, label: `${item.name} (${item.kind})` }))}
        bind:value={draft.provider}
        onchange={pickProvider}
      />
      <TextField label="Base URL" bind:value={draft.base_url} inputmode="url" error={urlError} />
      <TextField label="Model" bind:value={draft.model} error={modelError} />
      <div class="buttons">
        <Button variant="primary" type="submit" busy={saving}>Save changes</Button>
        <Button variant="secondary" onclick={() => (editing = false)}>Cancel</Button>
      </div>
    </form>
  {:else}
    <div class="primary">
      <div class="model">{settings.primary.model}</div>
      <div class="facts">
        <span>{main?.name ?? settings.primary.provider}</span>
        <Badge tone={main?.kind === 'local' ? 'dom' : 'neutral'}>{main?.kind === 'local' ? 'Local' : 'Cloud'}</Badge>
        {@render key(main)}
      </div>
      <div class="url">{settings.primary.base_url}</div>
      {#if main?.api_key_env && !main.key_set}
        <p class="fix">Add {main.api_key_env} to Dom's .env, then restart him.</p>
      {/if}
    </div>

    <div class="chain">
      <h3>If it fails</h3>
      {#if settings.fallbacks.length}
        <ol role="list">
          {#each settings.fallbacks as step, index (index)}
            {@const item = provider(step.provider)}
            <li>
              <span class="step num" aria-hidden="true">{index + 1}</span>
              <span class="step-text">
                <span class="step-model">{step.model}</span>
                <span class="muted">on {item?.name ?? step.provider}</span>
              </span>
              {#if item?.api_key_env && !item.key_set}{@render key(item)}{/if}
            </li>
          {/each}
        </ol>
      {:else}
        <p class="muted">No backups. If this model is down, this job waits until it's back.</p>
      {/if}
    </div>

    <div class="test">
      <Button variant="secondary" size="sm" busy={testing} onclick={test}>
        {testing ? 'Testing' : 'Test'}<span class="sr-only"> {info.title} model</span>
      </Button>
      <p class="result" role="status">
        {#if result}
          <span class={result.ok ? 'ok' : 'bad'}>
            <Icon name={result.ok ? 'check' : 'alert'} size={18} />
            {result.ok && result.latency_ms !== null ? `${result.message} in ${result.latency_ms} ms` : result.message}
          </span>
        {/if}
      </p>
    </div>
  {/if}
</Card>

<style>
  .job {
    color: var(--ink-muted);
    margin-top: calc(-1 * var(--space-2));
  }

  .primary {
    margin-top: var(--space-4);
    padding: var(--space-3) var(--space-4);
    border-radius: var(--radius-sm);
    background: var(--surface-sunk);
  }

  .model {
    font-family: var(--font-display);
    font-weight: 600;
    overflow-wrap: anywhere;
  }

  .facts {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }

  .url {
    margin-top: var(--space-1);
    color: var(--ink-muted);
    font-size: var(--step--1);
    overflow-wrap: anywhere;
  }

  .fix {
    margin-top: var(--space-2);
    color: var(--cherry);
    font-size: var(--step--1);
    font-weight: 700;
  }

  .chain {
    margin-top: var(--space-4);
  }

  h3 {
    font-size: var(--step--1);
    font-family: var(--font-body);
    font-weight: 700;
    color: var(--ink-muted);
  }

  ol {
    list-style: none;
    margin-top: var(--space-2);
    display: grid;
    gap: var(--space-2);
  }

  li {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }

  .step {
    display: grid;
    place-items: center;
    flex: none;
    width: 24px;
    height: 24px;
    border: 1.5px solid var(--line-strong);
    border-radius: 50%;
    font-size: var(--step--1);
    font-weight: 700;
  }

  .step-text {
    display: flex;
    flex-wrap: wrap;
    column-gap: var(--space-2);
    min-width: 0;
    flex: 1;
  }

  .step-model {
    font-weight: 700;
    overflow-wrap: anywhere;
  }

  .chain > p {
    margin-top: var(--space-1);
    font-size: var(--step--1);
  }

  .test {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    margin-top: var(--space-5);
  }

  .result {
    font-size: var(--step--1);
    font-weight: 700;
  }

  .result span {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
  }

  .ok {
    color: var(--mint);
  }

  .bad {
    color: var(--cherry);
  }

  .edit {
    display: grid;
    gap: var(--space-4);
    margin-top: var(--space-4);
  }

  .buttons {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
</style>
