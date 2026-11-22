<script lang="ts">
  import type { DesktopContainer, HomeResponse, RecentTurn } from '../lib/api/types';
  import Badge from '../lib/components/Badge.svelte';
  import Bubble from '../lib/components/Bubble.svelte';
  import Button from '../lib/components/Button.svelte';
  import Card from '../lib/components/Card.svelte';
  import EmptyState from '../lib/components/EmptyState.svelte';
  import ListRow from '../lib/components/ListRow.svelte';
  import Stat from '../lib/components/Stat.svelte';
  import { ago, clock, count, day, usd } from '../lib/format';

  interface Props {
    data: HomeResponse;
  }

  let { data }: Props = $props();

  const HEADLINE = { online: "Dom's online", offline: "Dom's offline", asleep: "Dom's asleep" } as const;

  const where = $derived.by(() => {
    const { presence, online_in, sleep } = data.dom;
    if (presence === 'asleep') return `Sleeping until ${sleep.end}. He's out from ${sleep.start} every night.`;
    if (presence === 'offline') return `Not in any chat right now. He sleeps ${sleep.start} to ${sleep.end}.`;
    const spans = online_in.map(
      (span) => `${span.scope === 'owner_dm' ? 'your DM' : span.title} until ${clock(span.until)}`,
    );
    return `In ${spans.join(', and ')}.`;
  });

  const PICKED = { reflection: 'in last night’s reflection', random: 'at random this morning', switch: 'mid-day' } as const;

  const vibeNote = $derived.by(() => {
    const { vibe } = data;
    if (vibe.mode === 'locked') return `Locked to ${vibe.preset?.title ?? 'no preset'} by you.`;
    if (vibe.mode === 'none') return 'No vibe today: plain Dom everywhere.';
    return vibe.source ? `Today's vibe, picked ${PICKED[vibe.source]}.` : "Today's vibe.";
  });

  const OUTCOME: Record<RecentTurn['outcome'], string> = {
    replied: 'Replied',
    reacted: 'Reacted',
    quiet: 'Stayed quiet',
    held_back: 'Held back: possible leak',
    failed: 'Turn failed',
  };

  const DESKTOP_TONE: Record<DesktopContainer['tone'], 'neutral' | 'live' | 'waiting' | 'danger'> = {
    off: 'neutral',
    starting: 'neutral',
    running: 'live',
    waiting: 'waiting',
    refused: 'danger',
    error: 'danger',
  };

  const today = $derived(data.today);
  const reminder = $derived(today.next_reminder);
</script>

<div class="home">
  <section class="hero" aria-labelledby="home-title">
    <h1 id="home-title" class={data.dom.presence}>{HEADLINE[data.dom.presence]}</h1>
    <p class="where">{where}</p>
    {#if data.vibe.mode === 'auto' && data.vibe.reason}
      <figure class="vibe">
        <Bubble size="lg" typing>{data.vibe.reason}</Bubble>
        <figcaption>
          {#if data.vibe.preset}<Badge tone="dom">{data.vibe.preset.title}</Badge>{/if}
          <span>{vibeNote}</span>
        </figcaption>
      </figure>
    {:else}
      <p class="vibe-plain">{vibeNote}</p>
    {/if}
  </section>

  <div class="waiting">
    <Card
      title="Waiting for you"
      meta={data.approvals.length ? count(data.approvals.length, 'thing') : ''}
      tone={data.approvals.length ? 'waiting' : 'plain'}
      flush={data.approvals.length > 0}
    >
      {#if data.approvals.length}
        <ul role="list">
          {#each data.approvals as item (item.id)}
            <ListRow title={item.label} meta={item.summary}>
              {#snippet trail()}
                <Button size="sm" href="/proposals">Review<span class="sr-only">: {item.label}</span></Button>
              {/snippet}
            </ListRow>
          {/each}
        </ul>
      {:else}
        <EmptyState
          title="Nothing's waiting for you."
          body="Soul edits, new groups and plugin drafts show up here when Dom needs a yes."
        />
      {/if}
    </Card>
  </div>

  <div class="today">
    <Card title="Today">
      <div class="stats">
        <Stat label="Messages in" value={String(today.received)} />
        <Stat label="Replies" value={String(today.replies)} />
        <Stat
          label="Spend"
          value={usd(today.spend_usd)}
          fill={today.cap_usd > 0 ? today.spend_usd / today.cap_usd : null}
          hint={today.cap_usd > 0 ? `of a ${usd(today.cap_usd)} cap` : 'No daily cap'}
        />
        <Stat
          label="Next reminder"
          value={reminder ? clock(reminder.at) : 'None'}
          hint={reminder ? `${day(reminder.at, data.now) === 'today' ? '' : `${day(reminder.at, data.now)}: `}${reminder.text}` : ''}
        />
      </div>
    </Card>
  </div>

  <div class="recent">
    <Card title="Recent replies" flush={data.recent.length > 0}>
      {#if data.recent.length}
        <ul role="list">
          {#each data.recent as turn (turn.id)}
            <ListRow title={turn.chat_title} meta={ago(turn.at, data.now)}>
              {#snippet trail()}
                {#if turn.outcome === 'held_back' || turn.outcome === 'failed'}
                  <Badge tone="danger">{OUTCOME[turn.outcome]}</Badge>
                {:else if turn.outcome === 'quiet'}
                  <span class="quiet">{OUTCOME.quiet}</span>
                {/if}
              {/snippet}
              {#if turn.outcome === 'replied' || turn.outcome === 'reacted'}
                <Bubble>
                  {#if turn.outcome === 'reacted'}<span class="sr-only">Reacted with </span>{/if}{turn.text}
                </Bubble>
              {/if}
            </ListRow>
          {/each}
        </ul>
      {:else}
        <EmptyState title="No replies yet today." body="Dom replies when someone messages him." />
      {/if}
    </Card>
  </div>

  <div class="desktop">
    <Card
      title="Desktop"
      meta={data.desktop.enabled ? count(data.desktop.containers.length, 'container') : ''}
      flush={data.desktop.enabled}
    >
      {#if data.desktop.enabled}
        <ul role="list">
          {#each data.desktop.containers as box (box.name)}
            <ListRow title={box.name} meta={box.host}>
              {#snippet trail()}
                <Badge tone={DESKTOP_TONE[box.tone]}>{box.state}</Badge>
                <Button size="sm" variant={box.tone === 'waiting' ? 'primary' : 'secondary'} href="/desktop?container={encodeURIComponent(box.name)}">
                  Open<span class="sr-only"> {box.name}</span>
                </Button>
              {/snippet}
              {#if box.detail}<p class="detail">{box.detail}</p>{/if}
            </ListRow>
          {/each}
        </ul>
      {:else}
        <EmptyState
          title="The desktop is off."
          body="Give Dom his tiny computer by setting a desktop mode in config.toml, then restart him."
        />
      {/if}
    </Card>
  </div>
</div>

<style>
  .home {
    display: grid;
    gap: var(--space-4);
    grid-template-areas: 'hero' 'waiting' 'today' 'recent' 'desktop';
  }

  .hero {
    grid-area: hero;
    padding: var(--space-4) 0 var(--space-3);
  }
  .waiting {
    grid-area: waiting;
  }
  .today {
    grid-area: today;
  }
  .recent {
    grid-area: recent;
  }
  .desktop {
    grid-area: desktop;
  }

  h1 {
    font-size: var(--step-3);
    font-weight: 800;
    letter-spacing: -0.03em;
  }

  /* Live state gets the live colour. */
  h1.online {
    color: var(--mint);
  }

  .where {
    margin-top: var(--space-2);
    color: var(--ink-muted);
    max-width: 60ch;
  }

  .vibe {
    margin: var(--space-5) 0 0;
  }

  figcaption {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin-top: var(--space-3);
    color: var(--ink-muted);
    font-size: var(--step--1);
  }

  .vibe-plain {
    margin-top: var(--space-3);
    color: var(--ink-muted);
  }

  .stats {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-5) var(--space-4);
  }

  .quiet {
    color: var(--ink-muted);
    font-size: var(--step--1);
  }

  .detail {
    color: var(--ink-muted);
    font-size: var(--step--1);
  }

  @media (min-width: 960px) {
    .home {
      grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
      grid-template-areas:
        'hero hero'
        'waiting today'
        'recent desktop'
        'recent .';
      align-items: start;
      gap: var(--space-5);
    }

    .hero {
      padding: var(--space-3) 0 var(--space-5);
    }

    h1 {
      font-size: var(--step-4);
    }
  }
</style>
