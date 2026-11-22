/*
  The JSON the dashboard reads from and sends to Dom.

  This is the contract for the backend: each page has one GET that returns everything it shows, and
  each action is one small request. Until Dom serves these, the app renders the fixtures in
  `fixtures.ts`, which are typed against this file.

  Conventions
  - Times are ISO 8601 strings with an offset, in the owner's timezone. Clock times ("HH:MM") are local.
  - Every GET carries `now`, so relative times ("3 min ago") are computed against Dom's clock, not the phone's.
  - Secrets never appear. A key is described only by the variable that holds it and whether it is set.
*/

export type Presence = 'online' | 'offline' | 'asleep';

export const TRAIT_NAMES = [
  'sarcasm',
  'playfulness',
  'humor',
  'formality',
  'empathy',
  'enthusiasm',
  'slang',
  'emoji',
] as const;
export type TraitName = (typeof TRAIT_NAMES)[number];

export interface Preset {
  /** File name under DOM_HOME/presets, without `.md`. */
  name: string;
  title: string;
  description: string;
}

/** The preset used everywhere right now, and who chose it. */
export interface Vibe {
  /** `auto`: Dom picks his own. `locked`: the owner pinned one. `none`: no preset. */
  mode: 'auto' | 'locked' | 'none';
  preset: Preset | null;
  /** Dom's own words for why he picked it. Auto only; empty otherwise. */
  reason: string;
  /** How an auto pick happened. Empty unless mode is `auto`. */
  source: '' | 'reflection' | 'random' | 'switch';
  since: string | null;
}

/* ---- Home: GET /api/home ------------------------------------------------------------------------- */

export interface HomeResponse {
  now: string;
  dom: {
    /** Overall: `asleep` inside his sleep window, `online` when he is online in any chat, else `offline`. */
    presence: Presence;
    /** Chats he is online in right now, soonest to end first. */
    online_in: OnlineSpan[];
    sleep: { start: string; end: string };
  };
  vibe: Vibe;
  today: TodayCounts;
  /** Newest first, at most 10. */
  recent: RecentTurn[];
  /** Everything waiting for a yes, oldest first. */
  approvals: Approval[];
  desktop: DesktopTile;
}

export interface OnlineSpan {
  chat_id: number;
  title: string;
  scope: Chat['scope'];
  until: string;
}

export interface TodayCounts {
  received: number;
  replies: number;
  spend_usd: number;
  /** 0 means no cap. */
  cap_usd: number;
  next_reminder: { at: string; text: string } | null;
}

export interface RecentTurn {
  id: number;
  at: string;
  chat_id: number;
  chat_title: string;
  outcome: 'replied' | 'reacted' | 'quiet' | 'held_back' | 'failed';
  /** First bubble of the reply, the reaction emoji, or empty for quiet, held back and failed turns. */
  text: string;
}

export type ApprovalKind =
  | 'soul_edit'
  | 'memory_import'
  | 'memory_edit'
  | 'skill'
  | 'plugin'
  | 'group'
  | 'stranger_dm';

export interface Approval {
  id: string;
  kind: ApprovalKind;
  /** A sentence, e.g. "Soul edit, suggested in reflection". */
  label: string;
  /** One line of what it is, e.g. the first changed line or the group's name. */
  summary: string;
  at: string;
}

export interface DesktopTile {
  enabled: boolean;
  containers: DesktopContainer[];
}

export interface DesktopContainer {
  name: string;
  host: string;
  /** Words for the state, e.g. "running", "waiting for you". */
  state: string;
  tone: 'off' | 'starting' | 'running' | 'waiting' | 'refused' | 'error';
  /** The running task's brief, or empty. */
  detail: string;
}

/* ---- Chats: GET /api/chats, PATCH /api/chats/{id}, POST /api/chats/{id}/leave ---------------------- */

export interface ChatsResponse {
  now: string;
  vibe: Vibe;
  presets: Preset[];
  chats: Chat[];
}

export interface Chat {
  id: number;
  title: string;
  scope: 'owner_dm' | 'friend_dm' | 'group';
  presence: Presence;
  /** When the current presence ends, if it has an end. */
  until: string | null;
  unread: number;
  /** This chat's own preset name, or null to follow the vibe. */
  preset: string | null;
  sliders: Record<TraitName, TraitSlider>;
  /** Assistant mode for an important task. Owner DM only. */
  task: { reason: string; until: string } | null;
}

export interface TraitSlider {
  /** 0 to 1. */
  value: number;
  /** True when set for this chat; false when inherited from the soul and preset. */
  custom: boolean;
}

/** Only the fields being changed. A slider set to null goes back to the inherited value. */
export interface ChatPatch {
  preset?: string | null;
  sliders?: Partial<Record<TraitName, number | null>>;
}

/** PUT /api/vibe. A preset name locks it; `auto` hands the pick back to Dom; `none` clears it. */
export type VibeChoice = { mode: 'auto' } | { mode: 'none' } | { mode: 'locked'; preset: string };

/* ---- Settings: GET /api/settings, PUT /api/settings/models/{role}, POST .../test, PATCH /api/settings -- */

export type ModelRole = 'chat' | 'fallback' | 'media' | 'drafting';

export interface Provider {
  id: string;
  name: string;
  kind: 'local' | 'cloud';
  base_url: string;
  /** Name of the `.env` variable that holds the key, or null for an endpoint without authentication. */
  api_key_env: string | null;
  /** Whether that variable is set. The value itself is never sent. */
  key_set: boolean;
}

export interface Endpoint {
  /** A `Provider.id`. */
  provider: string;
  base_url: string;
  model: string;
}

export interface RoleSettings {
  role: ModelRole;
  primary: Endpoint;
  /** Tried in order when the primary fails. */
  fallbacks: Endpoint[];
}

export type ToggleKey = 'memory_self_edit' | 'openers' | 'ephemeral_replies' | 'leak_check';

export interface SettingsResponse {
  providers: Provider[];
  roles: RoleSettings[];
  /** Jev, the TypeSafe Decisions API. A classifier, not a chat model, so it is shown but not swappable. */
  triage: { model: string; base_url: string; key_set: boolean };
  toggles: Record<ToggleKey, boolean>;
}

/** Response to POST /api/settings/models/{role}/test: one real call to the role's primary. */
export interface ModelTest {
  ok: boolean;
  latency_ms: number | null;
  /** What happened, e.g. "Answered" or "Connection refused". */
  message: string;
}

/** Body of PATCH /api/settings. */
export type TogglePatch = Partial<Record<ToggleKey, boolean>>;

/** Every non-2xx response. */
export interface ApiError {
  /** What went wrong and what to do, ready to show. */
  message: string;
}
