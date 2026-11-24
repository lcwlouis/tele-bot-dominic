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
export type Role = "chat" | "fallback" | "media" | "drafting" | "search" | "escalation";
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
};
export type StudioLimits = Record<StudioScope, StudioTier> & { daily: number };
export type StudioLimitsView = {
  revision: string;
  limits: StudioLimits;
  owner: {
    kinds: { kind: StudioKind; left: number; on: boolean }[];
    daily_left: number;
    /** A studio item for the owner's DM is being made or sent now. */
    sending: boolean;
    /** When the gap between media ends, if it hasn't; like `now`, in Dom's time zone with its offset. */
    next_at: string | null;
    now: string;
  };
};

/** GET /api/memory, GET /api/memory/file and the memory writes (dom/web/api_memory.py). */
export type MemoryImportCard = { id: number; service: string; facts: { id: number; text: string }[] };
export type MemoryEdit = { id: number; kind: "soul_edit" | "memory_edit"; label: string; title: string; diff: string[] };
export type MemoryWaiting = { imports: MemoryImportCard[]; edits: MemoryEdit[] };
export type MemoryIndex = MemoryWaiting & {
  sections: { heading: string; files: { path: string; label: string }[] }[];
  services: { id: string; label: string }[];
  export_prompt: string;
  max_soul_request: number;
  max_soul_turns: number;
  max_soul_chat: number;
};
/** GET /api/search (dom/web/api_search.py): hits by kind; `full` is false for another chat's one-line preview. */
export type SearchData = {
  query: string;
  groups: { kind: string; label: string; hits: { title: string; text: string; href: string; at: string; full: boolean }[] }[];
};
export type MemoryFile = { file: string; label: string; readonly: boolean; limit: number | null; content: string; version: string };

/** GET /api/chats and the chat writes (dom/web/api_chats.py). Every write answers { message, ...ChatsData }. */
export type ChatSlider = { name: string; value: number; custom: boolean };
/** GET /api/chats/{id}/messages: full text only in the owner's DM (`full`), one line per message elsewhere. */
export type ChatMessages = { chat_id: number; full: boolean; messages: { who: string; mine: boolean; text: string; at: string }[] };
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
};

/** GET /api/work/* and the Work writes (dom/web/api_work.py). Every write answers { ok, message, ...the tab's data }. */
// steps_budget: the step budget the task started with (null on tasks from before v0.8.3); extra_steps: steps the owner added.
export type StepBudget = { steps_budget: number | null; extra_steps: number };
export type DeskTask = { id: number; status: string; steps: number; brief: string; error: string | null } & StepBudget;
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
} & StepBudget;
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
export type WaitingData = {
  count: number;
  kind: string;
  source: string;
  kinds: string[];
  sources: string[];
  chats: { id: number; label: string; title: string; detail: string; first: string }[];
  cards: WaitingCard[];
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
};
