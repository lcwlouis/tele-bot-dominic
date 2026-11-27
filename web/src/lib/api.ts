import type { Activity } from "$lib/components/Orbit.svelte";
import { toast } from "$lib/toast.svelte";

/** GET /api/home (dom/web/api.py). */
export type HomeData =
  | { setup: true }
  | {
      setup: false;
      now: { greeting: string; date: string; clock: string; iso: string };
      presence: { state: "online" | "asleep" | "offline"; title: string; detail: string };
      activity: Activity;
      provider_budget: string | null;
      vibe: { mode: "auto" | "locked" | "none"; title: string; description: string; reason: string; since: string };
      waiting: { count: number; items: { label: string; detail: string }[]; href: string };
      today: {
        turns: number;
        replies: number;
        spend: number;
        cap: number;
        hourly: number[];
        next_reminder: { when: string; text: string } | null;
      };
      recent: { chat: string; text: string; at: string }[];
      desktop: { enabled: boolean; task: { brief: string; status: string } | null };
      for_you?: ForYou;
      goals?: { active_count: number };
    };

export async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" }, signal });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`.trim());
  return (await response.json()) as T;
}

/** A failed write: the status and the server's `detail`, plus per-slot `errors` where the route gives them. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly errors: { index: number; error: string }[] = [],
  ) {
    super(message);
  }
}

/**
 * The write pattern every ported page uses: `send("/api/...", body)`.
 *
 * - Writes are POST with a JSON body. Dom's middleware (dom/web/app.py) lets a POST through only when it is
 *   authenticated, its path is listed in a JSON_POSTS set, its Content-Type is application/json and its Origin is
 *   Dom's own; a same-origin fetch sends that Origin itself. Add each new write route to JSON_POSTS.
 * - On a 4xx/5xx or a network failure it shows the server's `detail` (or a plain message) as an error toast, then
 *   throws an ApiError so the caller can put things back or mark the failing field. Pass `quiet` when the caller
 *   shows the error itself.
 * - Settings writes take the GET's `revision`; a 409 means the file moved under the page: reload, then retry.
 * - Optimistic UI only where a write cannot fail on content (a switch): flip at once, flip back in the catch.
 *   Anything the server checks or tests (a provider) waits for the answer.
 */
export async function send<T>(path: string, body: unknown, { quiet = false }: { quiet?: boolean } = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    return fail(new ApiError(0, "Couldn't reach Dom. Check the connection and try again."), quiet);
  }
  if (response.ok) {
    try {
      return (await response.json()) as T;
    } catch {
      return fail(new ApiError(response.status, "Dom answered with something that isn't JSON. Reload and try again."), quiet);
    }
  }
  if (response.status === 403) return fail(new ApiError(403, "Dashboard access expired. Reload to sign in."), quiet);
  let result: { detail?: unknown; errors?: { index: number; error: string }[] } = {};
  try {
    result = await response.json();
  } catch {
    // a plain-text error page: fall back to the status below
  }
  const detail = typeof result.detail === "string" ? result.detail : `Request failed (${response.status}).`;
  return fail(new ApiError(response.status, detail, Array.isArray(result.errors) ? result.errors : []), quiet);
}

function fail(error: ApiError, quiet: boolean): never {
  if (!quiet) toast.show("error", error.message);
  throw error;
}

/** GET/POST /api/models and /api/models/test (dom/web/model_settings.py). */
export type Role = "chat" | "fallback" | "media" | "drafting" | "search" | "escalation" | "embedding";
/** The routing roles plus the decision roles (triage), which have one endpoint and no effort. */
export type SettingsRole = Role | "triage";
export type SearchProviderId = "searxng" | "openrouter" | "deepseek" | "tavily";
/** A provider's own effort names (low, medium, high, and any others it lists). */
export type Effort = string;
/** The service a provider talks to (dom/config.py ProviderKind). */
export type ProviderKind = "openai" | "openrouter" | "gemini" | "searxng" | "deepseek" | "tavily";
/** What a provider is for: model roles, studio backups, triage or web search (dom/config.py ProviderType). */
export type ProviderType = "chat" | "media" | "decisions" | "search";

/** What OpenRouter endpoints may do with a studio backup's request: zdr is the default (dom/config.py). Chat routes
 * always use zdr. */
export type DataPolicy = "zdr" | "no_training" | "any";

export type Endpoint = {
  base_url: string;
  model: string;
  api_key_env: string | null;
  reasoning_effort: Effort | null;
  provider: SearchProviderId | null;
};

export type ProviderView = Endpoint & {
  kind: "local" | "cloud";
  key_set: boolean;
  data_retention: string | null;
  policy_url: string | null;
  policy_label: string | null;
};

export type SearchProviderInfo = {
  id: SearchProviderId;
  label: string;
  default_base_url: string;
  default_api_key_env: string;
  needs_model: boolean;
  needs_key: boolean;
  data_retention: string;
  policy_url: string | null;
  policy_label: string | null;
};

/** A saved `[providers.<name>]` table, or (saved: false) an address a role uses that isn't saved yet. */
export type ProviderSetup = {
  name: string;
  kind: ProviderKind;
  type: ProviderType;
  base_url: string;
  api_key_env: string | null;
  key_set: boolean;
  is_local: boolean;
  saved: boolean;
  listable: boolean;
  /** A role's slot, or a studio backup chain's (voice, image, video). */
  uses: { role: SettingsRole | StudioChain; index: number }[];
};

export type ModelSettings = {
  revision: string;
  roles: Record<SettingsRole, { primary: ProviderView | null; fallbacks: ProviderView[] }>;
  providers: ProviderSetup[];
  provider_presets: { name: string; kind: ProviderKind; type: ProviderType; base_url: string; api_key_env: string | null; model: string | null }[];
  /** The studio's cloud backups ([[studio.<chain>_fallbacks]]), in the order they're tried. */
  studio: Record<StudioChain, StudioFallback[]>;
  search_providers: SearchProviderInfo[];
  restart_required: boolean;
};

export type StudioChain = "voice" | "image" | "video";
export type StudioProvider = "openrouter_tts" | "gemini_tts" | "openrouter_image" | "openrouter_video";
/** One backup as POST /api/studio/save takes it (dom/web/studio_settings.py). */
export type StudioBackup = {
  provider: StudioProvider;
  model: string;
  api_key_env: string;
  paid: boolean;
  /** A voice backup's provider voice name; null uses the default. */
  voice: string | null;
  supports_reference: boolean;
  allow_without_reference: boolean;
  data_policy: DataPolicy;
};
export type StudioFallback = StudioBackup & { key_set: boolean };

/** POST /api/providers/models (dom/web/provider_settings.py): reasoning null means the provider doesn't say. */
export type ListedModel = { id: string; name: string; reasoning: boolean | null; efforts: string[]; default_effort?: string | null };
export type ModelList = { listable: boolean; models: ListedModel[]; fetched_at: number | null };

export type TestResult = { ok: boolean; latency_ms: number; reply: string | null; error: string | null; provider: string | null };

/** GET/POST /api/permissions (dom/web/model_settings.py). */
/** available is false when nothing set up can serve a studio switch; needs says what to set up first. */
export type Permission = {
  id: string;
  label: string;
  description: string;
  enabled: boolean;
  available: boolean;
  needs: string | null;
};
/** GET/POST /api/assistant (dom/web/assistant_settings.py): assistant mode's schedule and each message's last outcome. */
export type AssistantMessage = {
  id: "morning" | "evening";
  time: string; // "HH:MM" in the owner's timezone, empty when that message is off
  last: { day: string; at: string; state: string; label: string } | null;
};
export type AssistantView = { revision: string; timezone: string; enabled: boolean; messages: AssistantMessage[] };
export type ProfileInterval = 1 | 7 | 14 | 30;
export type Permissions = {
  revision: string;
  items: Permission[];
  desktop_steps: { value: number; min: number; max: number };
  profile_photo_every_days: ProfileInterval;
};

/** GET/POST /api/studio/limits (dom/web/studio_settings.py): `[studio.limits]` and what is left in the owner's DM. */
export type StudioScope = "owner_dm" | "friend_dm" | "group";
export type StudioKind = "voice" | "video_note" | "photo" | "special";
/** A daily video or image limit wins when set; null uses the weekly one. */
export type StudioTier = {
  voice_daily: number;
  video_daily: number | null;
  video_weekly: number;
  image_daily: number | null;
  image_weekly: number;
  special_daily: number;
  gap_minutes: number;
  /** A gap of the kind's own; null uses `gap_minutes`. */
  voice_gap_minutes: number | null;
  video_gap_minutes: number | null;
  image_gap_minutes: number | null;
  special_gap_minutes: number | null;
};
export type StudioLimits = Record<StudioScope, StudioTier> & { daily: number };
/** Where one chat stands against the limits of its scope; GET /api/chats/{id}/media, and `owner` of the limits view. */
export type MediaStanding = {
  kinds: {
    kind: StudioKind;
    left: number;
    /** This chat's items of the kind inside its rolling window. */
    used: number;
    per: "day" | "week";
    on: boolean;
    next_at: string | null;
  }[];
  daily_left: number;
  /** A studio item for this chat is being made or sent now. */
  sending: boolean;
  /** When the gap between media ends, if it hasn't; like `now`, in Dom's time zone with its offset. */
  next_at: string | null;
  now: string;
};
/** How readily Dom adds media on his own (`studio.spontaneity`): it scales every per-chat limit and gap, inside the global daily total. */
export type Spontaneity = "rarely" | "sometimes" | "often";
export type StudioLimitsView = {
  revision: string;
  limits: StudioLimits;
  spontaneity: Spontaneity;
  owner: MediaStanding;
};

/** GET /api/studio/event-days and POST /api/studio/event-days/cancel: days in Dom's life that raise his media limits. */
export type StudioEventDay = {
  id: number;
  /** A date in Dom's time zone. */
  day: string;
  title: string;
  multiplier: number;
  cancelled: boolean;
  /** Today, and not cancelled. */
  active: boolean;
};
export type StudioEventDays = { events: StudioEventDay[]; ceiling: number; per_week: number };

/** GET /api/memory, GET /api/memory/file and the memory writes (dom/web/api_memory.py). */
export type MemoryImportCard = { id: number; service: string; facts: { id: number; text: string }[] };
export type MemoryEdit = { id: number; kind: "soul_edit" | "memory_edit"; label: string; title: string; diff: string[] };
export type MemoryWaiting = { imports: MemoryImportCard[]; edits: MemoryEdit[] };
export type MemoryIndex = MemoryWaiting & {
  /** `archive`: the journal archive and the former files, read-only and folded by default. */
  sections: { heading: string; archive: boolean; files: { path: string; label: string }[] }[];
  /** Every chat's name by ID, for the preview's source chips ({chat -100123} reads "from: Weekend crew"). */
  chat_names: Record<string, string>;
  services: { id: string; label: string }[];
  export_prompt: string;
  max_soul_request: number;
  max_soul_turns: number;
  max_soul_chat: number;
};
/** GET /api/search (dom/web/api_search.py): hits by kind; `full` is false for another chat's one-line preview. */
export type SearchData = {
  query: string;
  mode?: "keyword" | "meaning" | "entity";
  groups: { kind: string; label: string; hits: SearchHit[] }[];
};
/** One hit. A meaning search adds `score` (higher is closer) and `match`, a coarse "strong", "good" or "weak";
 * keyword hits and older servers send neither. */
export type SearchHit = {
  title: string;
  text: string;
  href: string;
  at: string;
  full: boolean;
  score?: number | null;
  match?: string | null;
};
/** `entities` names each `kind:key` the file's `{entities ...}` groups link ("person:7" → "Robin"); older servers omit it. */
export type MemoryFile = { file: string; label: string; readonly: boolean; limit: number | null; content: string; version: string; entities?: Record<string, string> };
/** GET /api/memory/core: every core memory, with who pinned it ("owner" or "dom"), the day and Dom's reason. */
export type CoreMemory = { file: string; text: string; source: string; by: "owner" | "dom"; day: string; why: string; raw?: string };
export type CoreMemories = { cap: number; items: CoreMemory[] };

/** GET /api/chats and the chat writes (dom/web/api_chats.py). Every write answers { message, ...ChatsData }. */
export type ChatSlider = { name: string; value: number; custom: boolean };
/** GET /api/chats/{id}/messages: full text only in the owner's DM (`full`), one line per message elsewhere. */
export type ChatMessages = {
  chat_id: number;
  full: boolean;
  /** `at` is a short local time. The owner's DM (full) adds what its bubbles need, and on Dom's messages the turn
   *  that sent them (null when none was running). */
  messages: ({ id: number; who: string; mine: boolean; text: string; at: string } & Partial<{
    iso: string;
    origin: string | null;
    edited: boolean;
    media: TalkMessage["media"];
    turn: MessageTurn | null;
  }>)[];
  /** The cursor for the next older page (GET …/messages?before=…), null at the start of the chat. */
  next_before: number | null;
  has_more: boolean;
};
/** The turn behind one of Dom's messages: tokens over every call of the turn, its cost from the usage rows written
 *  while it ran (null when none was), and the attention lane it ran in (null before the turn log kept lanes). */
export type MessageTurn = {
  id: number;
  model: string;
  fallback: boolean;
  calls: number;
  input_tokens: number | null;
  cached_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  duration_ms: number;
  first_bubble_ms: number | null;
  lane: string | null;
};
/** GET /api/chats/unreachable: chats the current bot can no longer reach (E74). Their history is kept. */
export type UnreachableChat = {
  id: number;
  title: string;
  scope: "owner_dm" | "friend_dm" | "group";
  prior_status: string;
  reason: string | null;
  since: string;
  hidden: boolean;
};
export type Chat = {
  id: number;
  title: string;
  scope: "owner_dm" | "friend_dm" | "group";
  state: "online" | "asleep" | "offline";
  until: string;
  unread: number;
  preset: { name: string; title: string } | null;
  own_preset: string;
  task: { reason: string; until: string } | null;
  sliders: ChatSlider[];
};
export type ChatsData = {
  chats: Chat[];
  presets: { name: string; title: string; description: string }[];
  everywhere: string;
  pending: number;
  vibe: { mode: "auto" | "locked" | "none"; title: string; description: string; reason: string; source: string; since: string };
  stickers: string[];
  unreachable: UnreachableChat[];
};

/** GET /api/work/* and the Work writes (dom/web/api_work.py). Every write answers { ok, message, ...the tab's data }. */
// steps_budget: the step budget the task started with (null on tasks from before v0.8.3); extra_steps: steps the owner added.
export type StepBudget = { steps_budget: number | null; extra_steps: number };
// guest_chat and guest_asker are set only on a task that a group or a friend asked for (after the owner's OK).
export type GuestOrigin = { guest_chat?: string; guest_asker?: string };
export type DeskTask = { id: number; status: string; steps: number; brief: string; error: string | null; created_at: string } & StepBudget & GuestOrigin;
export type DeskContainer = {
  name: string;
  host: string;
  status: string;
  state: string;
  tone: "waiting" | "starting" | "running" | "refused" | "error" | "off";
  detail: string;
  selected: boolean;
  task: { id: number; status: string } | null;
};
export type DeskHistory = {
  id: number;
  status: string;
  container: string;
  steps: number;
  brief: string;
  result: string;
  error: string;
  finished: string;
  created_at: string;
  finished_at: string | null;
} & StepBudget & GuestOrigin;
export type DeskData = {
  enabled: boolean;
  status: string;
  container: string | null;
  on_this_machine: boolean;
  error: string | null;
  handoff_host: string | null;
  task: DeskTask | null;
  viewer: string;
  containers: DeskContainer[];
  hosts: { name: string; running: number; max_containers: number; memory_floor: string }[];
  history: DeskHistory[];
};
export type OwnTask = {
  id: number;
  goal: string;
  why: string;
  status: string;
  tools: string[];
  sites: string[];
  minutes: number;
  steps_budget: number;
  steps: number;
  announced: string;
  starts: string;
  finished: string;
  result: string;
  error: string;
  files: string[];
  announced_at: string;
  finished_at: string | null;
};
export type Skill = { name: string; description: string; body: string; hash: string; retired: boolean };
export type OwnData = {
  enabled: boolean;
  active: boolean;
  delay_hours: number;
  guard: { ok: boolean; detail: string; at: string } | null;
  tasks: OwnTask[];
  skills: Skill[];
};
export type Reminder = {
  id: number;
  due: string;
  importance: string;
  repeat: string | null;
  status: string;
  undeliverable: boolean;
  text: string;
  created_at: string;
};
export type RemindersData = { reminders: Reminder[] };
export type Brief = { filename: string; title: string; markdown: string; blocks: { kind: "heading" | "code" | "ul" | "ol" | "p"; lines: string[]; language: string }[] };
export type BriefsData = { briefs: Brief[] };

/** GET /api/waiting and its writes (dom/web/api_waiting.py). Every write answers { ok, message, ...WaitingData }. */
export type Trial = { label: string; elapsed_ms: number; failed: boolean; detail: string };
export type WaitingCard =
  | { type: "import"; id: number; service: string; facts: { id: number; text: string }[] }
  | ({ type: "proposal"; id: number; kind: string; label: string; title: string } & {
      diff?: string[];
      description?: string;
      text?: string;
      network?: boolean;
      storage?: boolean;
      test_run?: Trial | null;
      review?: string;
      old?: string;
      new?: string;
      address_name?: string;
      places?: string[];
      macs?: string[];
      reason?: string;
      leak_score?: number | null;
    });
export type DesktopAction =
  | "visit_site"
  | "fill_login"
  | "download"
  | "upload"
  | "background_work"
  | "calendar_create"
  | "calendar_add"
  | "calendar_move"
  | "calendar_delete"
  | "gmail_draft"
  | "doc_create"
  | "doc_append"
  | "sheet_create"
  | "sheet_append"
  | "sheet_update"
  | "guest_desktop";
export type PermissionPolicy = "ask" | "allow" | "never";
export type SavedLogin = {
  id: number;
  site: string;
  label: string;
  username_hint: string;
  has_username: boolean;
  has_password: boolean;
  match_subdomains: boolean;
  created_at: string;
  updated_at: string;
};
export type OwnerSecret = { key: string; label: string; created_at: string; updated_at: string };
export type DesktopActionItem = {
  id: DesktopAction;
  label: string;
  description: string;
  site_scoped: boolean;
  asks_every_time: boolean;
  policy: PermissionPolicy;
  site_allow_count: number;
};
export type SiteRule = { site: string; action: "visit_site" | "fill_login" | "download" | "upload"; policy: PermissionPolicy; updated_at: string };
export type PendingPermission = {
  id: number;
  task_id: number | null;
  self_task_id: number | null;
  action: DesktopAction;
  action_label: string;
  google: boolean;
  site: string;
  detail: string;
  created_at: string;
  can_allow_always?: boolean;
  dashboard_only?: boolean;
};
export type PermissionHistoryRow = {
  id: number;
  task_id: number | null;
  self_task_id: number | null;
  action: DesktopAction;
  action_label: string;
  google: boolean;
  site: string;
  detail: string;
  status: string;
  decided_via: string;
  created_at: string;
  decided_at: string | null;
};
export type PendingSecretRequest = {
  id: number;
  label: string;
  reason: string;
  target: "secret" | "env" | "login";
  target_key: string;
  created_at: string;
  expires_at: string;
};
export type SecretRequestLookup = PendingSecretRequest & { overwrites_existing: boolean };
export type AccessData = {
  key_status: { configured: boolean; key_id: string | null; previous_present: boolean; total_entries: number; error: string };
  logins: SavedLogin[];
  owner_secrets: OwnerSecret[];
  actions: DesktopActionItem[];
  policies: Record<DesktopAction, PermissionPolicy>;
  site_rules: SiteRule[];
  pending_permissions: PendingPermission[];
  history: PermissionHistoryRow[];
  pending_secret_requests: PendingSecretRequest[];
};

export type WaitingData = {
  count: number;
  kind: string;
  source: string;
  kinds: string[];
  sources: string[];
  chats: { id: number; label: string; title: string; detail: string; first: string }[];
  cards: WaitingCard[];
  desktop_permissions?: PendingPermission[];
  secret_requests?: PendingSecretRequest[];
  settled: { id: number; label: string; status: string; title: string; created_at: string }[];
  notices: { network: string; storage: string };
};

/** GET /api/system/* and the plugin writes (dom/web/api_system.py). */
export type PluginRow = { name: string; status: string; ended: "rejected" | "retired" | null; hash: string; available: boolean; network: boolean; storage: boolean; test_run: Trial | null };
export type PluginsData = {
  plugins: PluginRow[];
  panels: { plugin: string; id: string; title: string }[];
  notices: { network: string; storage: string };
};
export type PluginReview = {
  name: string;
  description: string;
  status: string;
  hash: string;
  network: boolean;
  storage: boolean;
  surfaces_changed: boolean;
  reviewed: boolean;
  test_run: Trial | null;
  tools: { name: string; scopes: string[] }[];
  commands: { name: string; description: string; scopes: string[] }[];
  panels: { id: string; title: string }[];
  documents: { path: string; removed: boolean; text: string; changes: string[] }[];
  notices: { network: string; storage: string };
};
export type PanelResult = {
  plugin: string;
  id: string;
  error: string | null;
  title: string;
  paragraphs: string[];
  pairs: string[][];
  columns: string[];
  rows: string[][];
};
export type Breakdown = { name: string; calls: number; spend: number }[];
export type UsageTokens = { calls: number; spend: number; input_tokens: number; output_tokens: number };
/** One kind's share of a day's bar, with what a tap on it shows. */
export type UsagePart = UsageTokens & { kind: string; y: number; height: number; models: (UsageTokens & { model: string })[] };
export type UsageData = {
  spent: number;
  cap: number;
  ratio: number;
  tone: "arc" | "amber" | "signal";
  kinds: Record<string, string>;
  fallbacks: number;
  today_kinds: Breakdown;
  today_models: Breakdown;
  month_kinds: Breakdown;
  month_models: Breakdown;
  bars: { date: string; spent: number; fallbacks: number; parts: UsagePart[] }[];
};
export type ModelCall = {
  kind: string;
  model: string;
  host: string | null;
  fallback: boolean | null;
  duration_ms: number;
  status: string | number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_tokens: number | null;
  gap_ms: number | null;
  between_tokens: number | null;
  system_hash: string | null;
  tools_hash: string | null;
  user_hash: string | null;
  user_prefix_hash: string | null;
};
export type Turn = {
  id: number;
  chat_id: string;
  /** Whether the debug tracer has this turn's full trace (traces are kept for `tracer.retention_days`). */
  has_trace: boolean;
  at: string;
  time: string;
  full_time: string;
  title: string;
  outcome: string;
  first_bubble_ms: number | null;
  last_bubble_ms: number | null;
  duration_ms: number;
  model: string;
  fallback: boolean;
  calls: number;
  steps: string[];
  /** Telegram refused a reaction on this turn, with its reasons (one per refused reaction). */
  reaction_rejected: boolean;
  reaction_reasons: string[];
  /** The compose lane the turn ran in, and how long it waited for its token; null when it never reached the attention loop. */
  lane: "main" | "fast" | null;
  queue_wait_ms: number | null;
  visits: { reason: string; behind: string; capped: string }[];
  timing: { label: string; ms: number }[];
  segments: { stage: string; label: string; x: number; ms: number }[];
  bar_ms: number;
  model_calls: ModelCall[];
};
export type TurnsData = {
  chat: string;
  stages: Record<string, string>;
  visible_stages: string[];
  choices: { id: string; title: string }[];
  visits: { title: string; duration_ms: number; depth: number; left: number; width: number; time: string }[];
  cache: { gap: string; returns: number; hit: number | null; between: number | null }[];
  cross_chat: { kind: string; source: string; target: string; status: string; drop_reason: string | null; created_at: string; queued_at: string | null; sending_at: string | null; updated_at: string; text: string }[];
  turns: Turn[];
  /** Reactions Telegram refused over the last `days` days, and the newest reasons. */
  reactions: { days: number; rejected: number; latest: { at: string; chat_id: string; reason: string }[] };
};

/** GET /api/health (dom/web/api_system.py): `boot` is new in every process, so a restarted Dom answers with another. */
export type Health = { ok: boolean; boot: string; version: string };
/** POST /api/system/restart: refused with 409 and the reason while a desktop task or a card would be lost. */
export type RestartResult = { ok: boolean; message: string; boot: string };

/** GET /api/spend and POST /api/spend/caps (dom/web/spend_settings.py). A null cap means none is set. */
export type SpendArea = "chat" | "media" | "search" | "desktop" | "memory";
export type Money = { spent: number; cap: number | null; left: number | null };
export type SpendView = {
  revision: string;
  timezone: string;
  today: Money;
  month: Money;
  month_label: string;
  month_ends: string;
  cap_owner_dm: boolean;
  areas: (Money & { area: SpendArea; label: string; help: string })[];
  daily: { date: string; spent: number; areas: Record<SpendArea, number> }[];
  help: Record<"daily_spend_cap_usd" | "monthly_spend_cap_usd" | "cap_owner_dm" | "area_caps", string>;
  one_off_room: number | null;
};

/** GET/POST /api/leak-scan (dom/web/spend_settings.py). `custom`: a legacy threshold between the levels. */
export type LeakLevelId = "strict" | "standard" | "relaxed" | "off";
export type LeakScanView = {
  revision: string;
  level: LeakLevelId | "custom";
  cutoff: number | null;
  levels: { id: LeakLevelId; label: string; help: string; cutoff: number | null }[];
  available: boolean;
  help: string;
};

/** GET /api/services and POST /api/services/save|test (dom/web/services_settings.py). A secret field never carries
 * its value: only `is_set`. */
export type ServiceState = "ok" | "missing" | "error" | "off";
export type ServiceField = {
  name: string;
  label: string;
  kind: "text" | "url" | "secret" | "bool" | "int";
  value: string | number | boolean | null;
  is_set: boolean | null;
  editable: boolean;
  help: string;
  min: number | null;
  max: number | null;
};
export type Service = {
  id: string;
  label: string;
  state: ServiceState;
  detail: string;
  help: string;
  fields: ServiceField[];
  links: { label: string; kind: "telegram" | "command"; text: string }[];
  permissions: { id: string; label: string; detail: string }[];
  children: Service[];
  can_test: boolean;
  restart_required: boolean;
};
export type ServicesView = { revision: string; services: Service[]; notices: { id: string; glyph: "info"; text: string }[] };
export type ServiceTestResult = { ok: boolean; detail: string; latency_ms: number };

/** GET /api/integrations/google and its writes (dom/web/api_google.py). The client id and secret are only ever set or
 * not set; the tokens never leave Dom. */
export type GoogleSwitch = {
  id: "calendar_read" | "calendar_write" | "gmail_read" | "gmail_drafts" | "drive_files" | "contacts_read";
  label: string;
  on: boolean;
  granted: boolean;
  /** A scope Google is asked for only when its switch is wanted: when not granted, "Reconnect to allow" asks for just that. */
  extra: boolean;
};
export type GoogleView = {
  client_id_set: boolean;
  client_secret_set: boolean;
  redirect_uri: string | null;
  status: "disconnected" | "connected" | "needs_reconnect";
  connected_at: string | null;
  last_error: string;
  /** Whether Google granted `calendar.app.created`: without it Dom cannot keep calendars of his own. */
  own_calendars_granted: boolean;
  calendars: { id: string; name: string }[];
  switches: GoogleSwitch[];
  /** Whether GOOGLE_PICKER_API_KEY and GOOGLE_PROJECT_NUMBER are set, so the file picker page can open. */
  picker_ready: boolean;
  picker_path: string;
};
/** GET /api/integrations/google/files: Dom's folder and the files he may see, names only. */
export type GoogleFiles = { folder: { name: string; url: string } | null; files: { name: string; kind: string }[]; more: boolean };
/** GET and POST /api/integrations/google/booking (dom/web/api_google.py, dom/booking.py): when the owner is willing to meet friends. */
export type BookingWindow = { days: ("mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun")[]; start: string; end: string };
export type BookingView = {
  windows: BookingWindow[];
  notice_hours: number;
  ahead_days: number;
  invite_friends: boolean;
  /** The owner's timezone, which the windows are in. */
  zone: string;
  /** The calendar bookings go to, and whether Dom has made it. */
  calendar: string;
  calendar_made: boolean;
  /** Calendar read and Dom's own calendars are both on. */
  ready: boolean;
};
/** GET /api/integrations/google/calendars: Dom's calendars as Google shows them now. `upcoming` is null when Google could not say. */
export type GoogleCalendar = { id: string; name: string; color: string; upcoming: number | null; more: boolean };

/** GET /api/goals and its writes (dom/web/api_goals.py). Every write answers { ok, message, ...GoalsData }. */
export type GoalStatus = "active" | "paused" | "done" | "dropped";
export type Goal = {
  id: number;
  text: string;
  category: string;
  target_date: string | null;
  check_in_rhythm: string;
  status: GoalStatus;
  progress_note: string;
  last_checkin_at: string | null;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
};
export type GoalsData = { goals: Goal[]; categories: string[]; rhythms: string[]; statuses: GoalStatus[] };

/** GET /api/noticings and its writes (dom/web/api_noticings.py), and Home's `for_you`. */
export type Noticing = {
  id: number;
  day: string;
  kind: "deadline" | "help" | "followup" | "idea";
  title: string;
  body: string;
  action_kind: string;
  source_label: string;
  status: "pending" | "accepted" | "dismissed";
  outcome: string;
  created_at: string;
};
export type NoticingsData = { noticings: Noticing[]; pending_count: number; kinds: string[]; statuses: string[] };
export type ForYou = { count: number; items: { id: number; kind: string; title: string; body: string; source_label: string; created_at: string }[] };

/** GET /api/media/files, /api/media/usage and /api/media/inputs (dom/web/api_media.py). */
export type MediaFile = {
  chat_id: number;
  chat: string;
  msg_id: number;
  kind: "photo" | "voice" | "video_note" | string;
  special: boolean;
  text: string;
  at: string;
  bytes: number | null;
  stored: boolean;
  url: string;
  still_url: string | null;
  /** Where it came from, from its linked usage rows; null for media made before 0.8.4. */
  made: MediaMade | null;
};
/** One generation: the cost adds up every linked call, so a cost reported late appears (`pending` until then). */
export type MediaPart = {
  source: "local" | "cloud";
  model: string;
  host: string | null;
  cost: number;
  pending: boolean;
  /** Every call behind it was the local studio, so it cost nothing. */
  free: boolean;
  redo: { cost: number; pending: boolean; free: boolean; kept: "first" | "second" } | null;
};
export type MediaMade = MediaPart & {
  outfit: string | null;
  preset: string | null;
  seconds: number | null;
  duration: number | null;
  voice: MediaPart | null;
  still: MediaPart | null;
};
export type MediaFiles = { files: MediaFile[]; next: string | null };
export type MediaUsage = { files: number; bytes: number; cap_bytes: number };
export type MediaInputs = {
  providers: { model: string; accepts: string[]; source: "config" | "list" | "probe" | "unknown" }[];
  route_accepts: string[];
};

/** GET /api/memory/index/estimate and /progress, POST …/confirm and …/cancel (dom/web/api_memory.py). */
export type IndexEstimate = { items: number; rough_seconds: number; model: string; staged: boolean; rebuild: boolean };
export type IndexProgress = { done: number; left: number; model: string; state: string; last_error: string; stale: boolean };

/** GET /api/world and the writes under /api/world (dom/web/api_world.py). The sheet itself saves through /api/memory/save. */
export type WorldPlace = { slug: string; name: string; note: string; photo: boolean };
export type WorldData = {
  sheet: { file: string; limit: number | null; content: string; version: string; entities: Record<string, string> };
  last_change: { day: string; reason: string } | null;
  interval_days: number;
  places: WorldPlace[];
  max_photo_mb: number;
};

/** One message in the owner DM, as the Talk page shows it. */
export type TalkMessage = {
  id: number;
  msg_id: number | null;
  mine: boolean;
  who: string;
  text: string;
  at: string;
  /** "dashboard" when the owner wrote it on the Talk page. */
  origin: string | null;
  edited: boolean;
  media: { kind: string; url: string | null; description: string | null } | null;
};

/** One stored trace part (a turn, the work after it, or background work), as listed beside the messages. */
export type TraceSummary = { id: number; turn_id: number | null; kind: string; label: string; at: string; outcome: string; size: number };

export type TalkData = {
  messages: TalkMessage[];
  has_more: boolean;
  typing: boolean;
  traced: boolean;
  turns?: TraceSummary[];
};

/** One event in a trace (dom/tracer.py); fields beyond these depend on `type`. */
export type TraceEvent = { type: string; at: string; t_ms: number; [field: string]: unknown };

export type TracePart = {
  version: number;
  turn_id: number | null;
  chat_id: string;
  scope: string;
  kind: "turn" | "after" | "background";
  label: string;
  started_at: string;
  ended_at: string;
  duration_ms: number;
  outcome: string;
  model: string;
  fallback: boolean;
  steps: string[];
  visits: { reason: string; duration_ms: number; behind: string | null; capped: string | null; depth: number }[];
  timings: { what: string; ms: number | null }[];
  first_bubble_ms: number | null;
  last_bubble_ms: number | null;
  events: TraceEvent[];
  dropped_events: number;
  redactions: number;
  media_stripped: number;
  truncated: boolean;
};
