<script lang="ts">
  import type { Actions } from '../lib/api/store.svelte';
  import { TRAIT_NAMES, type Chat, type ChatsResponse, type TraitName } from '../lib/api/types';
  import Badge from '../lib/components/Badge.svelte';
  import Bubble from '../lib/components/Bubble.svelte';
  import Button from '../lib/components/Button.svelte';
  import Card from '../lib/components/Card.svelte';
  import ConfirmDialog from '../lib/components/ConfirmDialog.svelte';
  import EmptyState from '../lib/components/EmptyState.svelte';
  import Icon from '../lib/components/Icon.svelte';
  import Presence from '../lib/components/Presence.svelte';
  import Select from '../lib/components/Select.svelte';
  import Slider from '../lib/components/Slider.svelte';
  import { clock, count, day } from '../lib/format';
  import { toast } from '../lib/toast.svelte';

  interface Props {
    data: ChatsResponse;
    actions: Pick<Actions, 'patchChat' | 'leaveChat' | 'setVibe'>;
    /** Chat whose controls start open. */
    openId?: number | null;
  }

  let { data, actions, openId = null }: Props = $props();

  // svelte-ignore state_referenced_locally
  let open = $state<number | null>(openId);
  let confirming = $state<{ chat: Chat; kind: 'leave' | 'reset' } | null>(null);
  let confirmOpen = $state(false);

  const TRAIT_LABEL: Record<TraitName, string> = {
    sarcasm: 'Sarcasm',
    playfulness: 'Playfulness',
    humor: 'Humour',
    formality: 'Formality',
    empathy: 'Empathy',
    enthusiasm: 'Enthusiasm',
    slang: 'Slang',
    emoji: 'Emoji',
  };

  const SCOPE = { owner_dm: 'Your DM', friend_dm: 'Friend DM', group: 'Group' } as const;

  const vibeTitle = $derived(data.vibe.preset?.title ?? 'no preset');
  const online = $derived(data.chats.filter((chat) => chat.presence === 'online').length);

  const vibeOptions = $derived([
    { value: 'auto', label: data.vibe.mode === 'auto' ? `Let Dom pick (today: ${vibeTitle})` : 'Let Dom pick' },
    ...data.presets.map((preset) => ({ value: `locked:${preset.name}`, label: preset.title })),
    { value: 'none', label: 'No preset' },
  ]);
  const vibeValue = $derived(data.vibe.mode === 'locked' ? `locked:${data.vibe.preset?.name}` : data.vibe.mode);

  const presetOptions = $derived([
    { value: '', label: `Follow the vibe (${vibeTitle})` },
    ...data.presets.map((preset) => ({ value: preset.name, label: preset.title })),
  ]);

  function status(chat: Chat): string {
    if (chat.task) return `Assistant mode for ${chat.task.reason}, until ${clock(chat.task.until)}`;
    if (!chat.until) return chat.presence === 'offline' ? 'Offline' : chat.presence === 'online' ? 'Online' : 'Asleep';
    const when = day(chat.until, data.now) === 'today' ? clock(chat.until) : `${clock(chat.until)} ${day(chat.until, data.now)}`;
    return `${chat.presence === 'online' ? 'Online' : chat.presence === 'asleep' ? 'Asleep' : 'Offline'} until ${when}`;
  }

  function presetTitle(name: string | null): string {
    return data.presets.find((preset) => preset.name === name)?.title ?? vibeTitle;
  }

  async function run(work: Promise<void>, done: string) {
    try {
      await work;
      toast(done);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'That didn’t go through. Try again.', 'error');
    }
  }

  function setVibe(value: string) {
    const choice = value.startsWith('locked:')
      ? ({ mode: 'locked', preset: value.slice(7) } as const)
      : ({ mode: value as 'auto' | 'none' } as const);
    run(actions.setVibe(choice), value === 'auto' ? 'Dom picks his own vibe again' : 'Vibe changed everywhere');
  }

  function setPreset(chat: Chat, value: string) {
    const preset = value || null;
    run(actions.patchChat(chat.id, { preset }), `${chat.title} now uses ${presetTitle(preset)}`);
  }

  function setSlider(chat: Chat, name: TraitName, value: number) {
    run(
      actions.patchChat(chat.id, { sliders: { [name]: value } }),
      `${TRAIT_LABEL[name]} set to ${Math.round(value * 100)} in ${chat.title}`,
    );
  }

  function ask(chat: Chat, kind: 'leave' | 'reset') {
    confirming = { chat, kind };
    confirmOpen = true;
  }

  function confirmed() {
    if (!confirming) return;
    const { chat, kind } = confirming;
    if (kind === 'leave') {
      run(actions.leaveChat(chat.id), `Left ${chat.title}`);
    } else {
      const sliders = Object.fromEntries(TRAIT_NAMES.map((name) => [name, null]));
      run(actions.patchChat(chat.id, { sliders }), `Sliders reset in ${chat.title}`);
    }
  }
</script>

<div class="chats">
  <header class="page-head">
    <h1>Chats</h1>
    <p class="muted">{count(data.chats.length, 'chat')}, {online} online</p>
  </header>

  <Card title="Vibe everywhere">
    {#if data.vibe.mode === 'auto' && data.vibe.reason}
      <div class="vibe-reason"><Bubble>{data.vibe.reason}</Bubble></div>
    {/if}
    <div class="vibe-select">
      <Select
        label="Preset everywhere"
        options={vibeOptions}
        value={vibeValue}
        hint="Chats with their own preset keep it."
        onchange={setVibe}
      />
    </div>
  </Card>

  {#if data.chats.length}
    <ul class="list" role="list">
      {#each data.chats as chat (chat.id)}
        {@const expanded = open === chat.id}
        {@const custom = TRAIT_NAMES.filter((name) => chat.sliders[name].custom).length}
        <li class="chat" class:expanded>
          <button
            class="summary"
            type="button"
            aria-expanded={expanded}
            aria-controls="chat-{chat.id}"
            onclick={() => (open = expanded ? null : chat.id)}
          >
            <Presence presence={chat.presence} showLabel={false} />
            <span class="name">
              <span class="title">{chat.title}</span>
              <span class="status">{status(chat)}</span>
            </span>
            <span class="facts">
              <Badge tone={chat.scope === 'owner_dm' ? 'dom' : 'neutral'}>{SCOPE[chat.scope]}</Badge>
              <span class="unread num" class:zero={chat.unread === 0}>
                {chat.unread}<span class="sr-only"> unread</span>
              </span>
            </span>
            <span class="chevron" aria-hidden="true"><Icon name="chevron" /></span>
          </button>

          {#if expanded}
            <div class="detail" id="chat-{chat.id}">
              <div class="preset">
                <Select
                  label="Preset"
                  options={presetOptions}
                  value={chat.preset ?? ''}
                  onchange={(value) => setPreset(chat, value)}
                />
              </div>
              <fieldset>
                <legend>
                  Personality
                  <span class="muted">{custom ? `${count(custom, 'slider')} set for this chat` : 'Same as everywhere'}</span>
                </legend>
                <div class="sliders">
                  {#each TRAIT_NAMES as name (name)}
                    <Slider
                      label={TRAIT_LABEL[name]}
                      value={chat.sliders[name].value}
                      custom={chat.sliders[name].custom}
                      onchange={(value) => setSlider(chat, name, value)}
                    />
                  {/each}
                </div>
              </fieldset>
              <div class="actions">
                <Button variant="secondary" size="sm" disabled={custom === 0} onclick={() => ask(chat, 'reset')}>
                  Reset sliders
                </Button>
                {#if chat.scope === 'group'}
                  <Button variant="danger" size="sm" onclick={() => ask(chat, 'leave')}>Leave group</Button>
                {/if}
              </div>
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {:else}
    <EmptyState
      title="Dom doesn't know any chats yet."
      body="Message him, or add him to a group. New groups wait for your approval on Home."
    />
  {/if}
</div>

<ConfirmDialog
  bind:open={confirmOpen}
  title={confirming?.kind === 'leave' ? `Leave ${confirming.chat.title}?` : `Reset sliders in ${confirming?.chat.title}?`}
  confirmLabel={confirming?.kind === 'leave' ? 'Leave group' : 'Reset sliders'}
  tone={confirming?.kind === 'leave' ? 'danger' : 'primary'}
  onconfirm={confirmed}
>
  {#if confirming?.kind === 'leave'}
    Dom leaves the group and stops reading it. Someone there has to add him again to undo this.
  {:else}
    Every slider in this chat goes back to the value set everywhere.
  {/if}
</ConfirmDialog>

<style>
  .chats {
    display: grid;
    gap: var(--space-4);
  }

  .page-head {
    padding: var(--space-4) 0 var(--space-2);
  }

  .page-head p {
    margin-top: var(--space-1);
  }

  .vibe-reason {
    margin-bottom: var(--space-4);
  }

  .vibe-select {
    max-width: 420px;
  }

  .list {
    list-style: none;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .chat + .chat {
    border-top: 1px solid var(--line);
  }

  .summary {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    width: 100%;
    min-height: 68px;
    padding: var(--space-3) var(--space-3) var(--space-3) var(--space-4);
    border: 0;
    background: transparent;
    text-align: left;
    cursor: pointer;
  }

  .summary:hover {
    background: var(--surface-sunk);
  }

  .summary:focus-visible {
    outline-offset: -3px;
  }

  .name {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
  }

  .title {
    font-weight: 700;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .status {
    color: var(--ink-muted);
    font-size: var(--step--1);
  }

  .facts {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }

  .unread {
    min-width: 28px;
    padding: 0 var(--space-2);
    border-radius: var(--radius-pill);
    background: var(--grape);
    color: var(--grape-ink);
    font-size: var(--step--1);
    font-weight: 700;
    line-height: 22px;
    text-align: center;
  }

  .unread.zero {
    background: transparent;
    color: var(--ink-muted);
    font-weight: 400;
  }

  .chevron {
    color: var(--ink-muted);
    transition: rotate var(--dur-med) var(--ease-out);
  }

  .expanded .chevron {
    rotate: 180deg;
  }

  .detail {
    display: grid;
    gap: var(--space-5);
    padding: var(--space-2) var(--space-4) var(--space-5);
    animation: open var(--dur-med) var(--ease-out);
  }

  .preset {
    max-width: 420px;
  }

  fieldset {
    margin: 0;
    padding: 0;
    border: 0;
    min-width: 0;
  }

  legend {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1) var(--space-3);
    align-items: baseline;
    margin-bottom: var(--space-2);
    padding: 0;
    font-family: var(--font-display);
    font-weight: 600;
  }

  legend .muted {
    font-family: var(--font-body);
    font-size: var(--step--1);
    font-weight: 400;
  }

  .sliders {
    display: grid;
    gap: var(--space-1) var(--space-6);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  /* Phones: the scope badge makes way for the title. */
  @media (max-width: 479px) {
    .facts :global(.badge) {
      display: none;
    }
  }

  @media (min-width: 720px) {
    .sliders {
      grid-template-columns: 1fr 1fr;
    }
  }

  @media (min-width: 960px) {
    .chats {
      gap: var(--space-5);
    }

    .page-head {
      padding-top: var(--space-3);
    }

    .detail {
      padding-left: calc(var(--space-4) + 10px + var(--space-3));
    }
  }

  @keyframes open {
    from {
      opacity: 0;
      translate: 0 -6px;
    }
  }
</style>
