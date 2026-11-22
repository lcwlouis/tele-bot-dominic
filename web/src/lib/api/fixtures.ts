/*
  Made-up data for the baseline, Storybook and the screenshot tests. Every name, chat and number here
  is invented. Chat ids are small on purpose: real Telegram ids must never appear in this public repo.
*/
import type {
  Chat,
  ChatsResponse,
  HomeResponse,
  ModelRole,
  ModelTest,
  SettingsResponse,
  TraitName,
  TraitSlider,
} from './types';
import { TRAIT_NAMES } from './types';

export const NOW = '2026-11-22T20:14:00+00:00';

const PRESETS = [
  { name: 'classic', title: 'Classic', description: "Dom's usual voice." },
  { name: 'assistant', title: 'Assistant', description: 'Clear, efficient and light on jokes.' },
  { name: 'gremlin', title: 'Gremlin', description: 'More bits, fewer full stops.' },
];

const VIBE = {
  mode: 'auto',
  preset: PRESETS[2]!,
  reason: 'slept like a rock, woke up choosing chaos',
  source: 'reflection',
  since: '2026-11-22T07:40:00+00:00',
} as const satisfies HomeResponse['vibe'];

export const home: HomeResponse = {
  now: NOW,
  dom: {
    presence: 'online',
    online_in: [
      { chat_id: 101, title: 'Book club (we read 0 books)', scope: 'group', until: '2026-11-22T21:40:00+00:00' },
      { chat_id: 1, title: 'You', scope: 'owner_dm', until: '2026-11-22T22:05:00+00:00' },
    ],
    sleep: { start: '01:30', end: '09:00' },
  },
  vibe: VIBE,
  today: {
    received: 48,
    replies: 17,
    spend_usd: 0.31,
    cap_usd: 1,
    next_reminder: { at: '2026-11-22T21:00:00+00:00', text: 'Water the basil' },
  },
  recent: [
    {
      id: 9031,
      at: '2026-11-22T20:11:00+00:00',
      chat_id: 101,
      chat_title: 'Book club (we read 0 books)',
      outcome: 'replied',
      text: 'nah season two is better and i will die on this hill',
    },
    { id: 9030, at: '2026-11-22T20:02:00+00:00', chat_id: 1, chat_title: 'You', outcome: 'replied', text: 'done, moved it to friday 9am' },
    { id: 9029, at: '2026-11-22T19:48:00+00:00', chat_id: 102, chat_title: 'Sunday league', outcome: 'reacted', text: '🤣' },
    { id: 9028, at: '2026-11-22T19:31:00+00:00', chat_id: 102, chat_title: 'Sunday league', outcome: 'quiet', text: '' },
    { id: 9027, at: '2026-11-22T18:57:00+00:00', chat_id: 103, chat_title: 'Flat 4B', outcome: 'held_back', text: '' },
  ],
  approvals: [
    {
      id: 'proposal-41',
      kind: 'soul_edit',
      label: 'Soul edit, suggested in reflection',
      summary: 'Add: you think pineapple on pizza is fine, actually',
      at: '2026-11-22T07:41:00+00:00',
    },
    {
      id: 'chat-104',
      kind: 'group',
      label: 'Group approval',
      summary: 'Climbing crew, added by Juno',
      at: '2026-11-22T17:20:00+00:00',
    },
    {
      id: 'plugin-weather-a1b2',
      kind: 'plugin',
      label: 'Plugin draft',
      summary: 'rain-check: warns the group before a rained-out match',
      at: '2026-11-22T19:05:00+00:00',
    },
  ],
  desktop: {
    enabled: true,
    containers: [
      { name: 'default', host: 'This Pi', state: 'running', tone: 'running', detail: 'Finding a cheaper flight for the trip' },
      { name: 'scratch', host: 'Studio', state: 'off', tone: 'off', detail: '' },
    ],
  },
};

function sliders(custom: Partial<Record<TraitName, number>> = {}, base = 0.5): Record<TraitName, TraitSlider> {
  return Object.fromEntries(
    TRAIT_NAMES.map((name) => [name, { value: custom[name] ?? base, custom: name in custom }]),
  ) as Record<TraitName, TraitSlider>;
}

const CHATS: Chat[] = [
  {
    id: 1,
    title: 'You',
    scope: 'owner_dm',
    presence: 'online',
    until: '2026-11-22T22:05:00+00:00',
    unread: 0,
    preset: null,
    sliders: sliders(),
    task: null,
  },
  {
    id: 101,
    title: 'Book club (we read 0 books)',
    scope: 'group',
    presence: 'online',
    until: '2026-11-22T21:40:00+00:00',
    unread: 3,
    preset: null,
    sliders: sliders({ sarcasm: 0.8, slang: 0.7 }),
    task: null,
  },
  {
    id: 102,
    title: 'Sunday league',
    scope: 'group',
    presence: 'offline',
    until: null,
    unread: 40,
    preset: 'classic',
    sliders: sliders({ enthusiasm: 0.9, emoji: 0.75 }),
    task: null,
  },
  {
    id: 103,
    title: 'Flat 4B',
    scope: 'group',
    presence: 'asleep',
    until: '2026-11-23T09:00:00+00:00',
    unread: 0,
    preset: null,
    sliders: sliders(),
    task: null,
  },
  {
    id: 201,
    title: 'Tobi',
    scope: 'friend_dm',
    presence: 'offline',
    until: null,
    unread: 1,
    preset: null,
    sliders: sliders({ empathy: 0.85, sarcasm: 0.2 }),
    task: null,
  },
];

export const chats: ChatsResponse = { now: NOW, vibe: VIBE, presets: PRESETS, chats: CHATS };

export const settings: SettingsResponse = {
  providers: [
    {
      id: 'homelab',
      name: 'Homelab',
      kind: 'local',
      base_url: 'http://127.0.0.1:8000/v1',
      api_key_env: null,
      key_set: false,
    },
    {
      id: 'openrouter',
      name: 'OpenRouter',
      kind: 'cloud',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_env: 'OPENROUTER_API_KEY',
      key_set: true,
    },
    {
      id: 'anthropic',
      name: 'Anthropic',
      kind: 'cloud',
      base_url: 'https://api.anthropic.com/v1',
      api_key_env: 'ANTHROPIC_API_KEY',
      key_set: false,
    },
  ],
  roles: [
    {
      role: 'chat',
      primary: { provider: 'homelab', base_url: 'http://127.0.0.1:8000/v1', model: 'default' },
      fallbacks: [
        { provider: 'openrouter', base_url: 'https://openrouter.ai/api/v1', model: 'deepseek/deepseek-v4.1-flash-1017' },
      ],
    },
    {
      role: 'fallback',
      primary: { provider: 'openrouter', base_url: 'https://openrouter.ai/api/v1', model: 'deepseek/deepseek-v4.1-flash-1017' },
      fallbacks: [],
    },
    {
      role: 'media',
      primary: { provider: 'openrouter', base_url: 'https://openrouter.ai/api/v1', model: 'qwen/qwen3.8-omni-flash' },
      fallbacks: [],
    },
    {
      role: 'drafting',
      primary: { provider: 'anthropic', base_url: 'https://api.anthropic.com/v1', model: 'claude-haiku-5-5' },
      fallbacks: [
        { provider: 'openrouter', base_url: 'https://openrouter.ai/api/v1', model: 'deepseek/deepseek-v4.1-flash-1017' },
        { provider: 'homelab', base_url: 'http://127.0.0.1:8000/v1', model: 'default' },
      ],
    },
  ],
  triage: { model: '~typesafe/jev-latest', base_url: 'https://openrouter.ai/api/alpha', key_set: true },
  toggles: { memory_self_edit: false, openers: false, ephemeral_replies: false, leak_check: true },
};

/** Stand-in answers for the "Test" button until Dom runs real test calls. */
export const tests: Record<ModelRole, ModelTest> = {
  chat: { ok: true, latency_ms: 840, message: 'Answered' },
  fallback: { ok: true, latency_ms: 1320, message: 'Answered' },
  media: { ok: true, latency_ms: 2110, message: 'Answered' },
  drafting: { ok: false, latency_ms: null, message: 'ANTHROPIC_API_KEY is not set in .env' },
};
