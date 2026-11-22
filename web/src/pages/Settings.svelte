<script lang="ts">
  import type { Actions } from '../lib/api/store.svelte';
  import type { SettingsResponse, ToggleKey } from '../lib/api/types';
  import Badge from '../lib/components/Badge.svelte';
  import Card from '../lib/components/Card.svelte';
  import Toggle from '../lib/components/Toggle.svelte';
  import { toast } from '../lib/toast.svelte';
  import RoleCard from './RoleCard.svelte';

  interface Props {
    data: SettingsResponse;
    actions: Pick<Actions, 'saveRole' | 'testRole' | 'setToggle'>;
  }

  let { data, actions }: Props = $props();

  const TOGGLES: { key: ToggleKey; label: string; description: string; on: string; off: string }[] = [
    {
      key: 'memory_self_edit',
      label: 'Let Dom edit his own memory',
      description: 'He proposes each change to his notes in your DM. Nothing changes until you approve it.',
      on: 'Dom can propose memory edits',
      off: "Dom can't edit his memory",
    },
    {
      key: 'openers',
      label: 'Let Dom start conversations',
      description: 'He may message you first, a couple of times a day at most.',
      on: 'Dom may message you first',
      off: 'Dom waits for you to message',
    },
    {
      key: 'ephemeral_replies',
      label: 'Private replies in groups',
      description: 'Dom can answer you inside a group so only you see it. Where he isn’t an admin, it goes to your DM.',
      on: 'Private replies on',
      off: 'Private replies off',
    },
    {
      key: 'leak_check',
      label: 'Check replies for leaks',
      description: 'Jev reads each reply before it goes out and holds back anything that might share a private note.',
      on: 'Leak check on',
      off: 'Leak check off',
    },
  ];

  async function flip(key: ToggleKey, value: boolean) {
    const item = TOGGLES.find((toggle) => toggle.key === key)!;
    try {
      await actions.setToggle(key, value);
      toast(value ? item.on : item.off);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Not saved. Try again.', 'error');
    }
  }
</script>

<div class="settings">
  <header class="page-head">
    <h1>Settings</h1>
    <p class="muted">Which model does each job, and what Dom may do on his own.</p>
  </header>

  <section aria-labelledby="models-title">
    <h2 id="models-title">Models</h2>
    <p class="muted intro">Each job has a main model and backups, tried in order. Keys stay in Dom's .env; this page only shows whether one is set.</p>
    <div class="roles">
      {#each data.roles as role (role.role)}
        <RoleCard settings={role} providers={data.providers} ontest={actions.testRole} onsave={actions.saveRole} />
      {/each}
      <Card title="Triage">
        <p class="muted">
          Jev decides whether Dom should reply, and checks replies for leaks. It's a classifier, not a chat model,
          so it can't be swapped.
        </p>
        <div class="jev">
          <span class="jev-model">{data.triage.model}</span>
          {#if data.triage.key_set}<Badge tone="live">Key set</Badge>{:else}<Badge tone="danger">Key missing</Badge>{/if}
        </div>
      </Card>
    </div>
  </section>

  <section aria-labelledby="behaviour-title">
    <h2 id="behaviour-title">What Dom may do</h2>
    <ul class="toggles" role="list">
      {#each TOGGLES as item (item.key)}
        <li>
          <Toggle
            label={item.label}
            description={item.description}
            checked={data.toggles[item.key]}
            onchange={(value) => flip(item.key, value)}
          />
        </li>
      {/each}
    </ul>
  </section>
</div>

<style>
  .settings {
    display: grid;
    gap: var(--space-6);
  }

  .page-head {
    padding: var(--space-4) 0 0;
  }

  .page-head p {
    margin-top: var(--space-1);
  }

  .intro {
    margin: var(--space-1) 0 var(--space-4);
    max-width: 62ch;
  }

  .roles {
    display: grid;
    gap: var(--space-4);
  }

  .jev {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin-top: var(--space-3);
  }

  .jev-model {
    font-family: var(--font-display);
    font-weight: 600;
    overflow-wrap: anywhere;
  }

  .toggles {
    list-style: none;
    margin-top: var(--space-4);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
  }

  .toggles li {
    padding: var(--space-4);
  }

  .toggles li + li {
    border-top: 1px solid var(--line);
  }

  @media (min-width: 960px) {
    .page-head {
      padding-top: var(--space-3);
    }

    .roles {
      grid-template-columns: 1fr 1fr;
      align-items: start;
      gap: var(--space-5);
    }
  }
</style>
