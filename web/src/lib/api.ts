import type { Activity } from "$lib/components/Orbit.svelte";
import { toast } from "$lib/toast.svelte";

/** GET /api/home (dom/web/api.py). */
export type HomeData =
  | { setup: true }
  | {
      setup: false;
      now: { greeting: string; date: string; clock: string };
      presence: { state: "online" | "asleep" | "offline"; title: string; detail: string };
      activity: Activity;
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
export type SearchProviderId = "searxng" | "openrouter" | "deepseek" | "tavily";
export type Effort = "low" | "medium";

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

export type ModelSettings = {
  revision: string;
  roles: Record<Role, { primary: ProviderView | null; fallbacks: ProviderView[] }>;
  triage: { model: string; base_url: string; api_key_env: string | null; key_set: boolean };
  search_providers: SearchProviderInfo[];
  restart_required: boolean;
};

export type TestResult = { ok: boolean; latency_ms: number; reply: string | null; error: string | null; provider: string | null };

/** GET/POST /api/permissions (dom/web/model_settings.py). */
export type Permission = { id: string; label: string; description: string; enabled: boolean };
export type Permissions = { revision: string; items: Permission[] };

/** GET /api/memory, GET /api/memory/file and the memory writes (dom/web/api_memory.py). */
export type MemoryImportCard = { id: number; service: string; facts: { id: number; text: string }[] };
export type MemoryEdit = { id: number; kind: "soul_edit" | "memory_edit"; label: string; title: string; diff: string[] };
export type MemoryWaiting = { imports: MemoryImportCard[]; edits: MemoryEdit[] };
export type MemoryIndex = MemoryWaiting & {
  sections: { heading: string; files: { path: string; label: string }[] }[];
  services: { id: string; label: string }[];
  max_soul_request: number;
};
export type MemoryFile = { file: string; label: string; readonly: boolean; limit: number | null; content: string; version: string };

/** GET /api/chats and the chat writes (dom/web/api_chats.py). Every write answers { message, ...ChatsData }. */
export type ChatSlider = { name: string; value: number; custom: boolean };
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
export type DeskTask = { id: number; status: string; steps: number; brief: string; error: string | null };
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
};
export type DeskData = {
  enabled: boolean;
  status: string;
  container: string | null;
  local: boolean;
  error: string | null;
  handoff_host: string | null;
  task: DeskTask | null;
  viewer: string;
  containers: DeskContainer[];
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
export type Brief = { filename: string; title: string; markdown: string; blocks: { kind: "heading" | "code" | "ul" | "ol" | "p"; lines: string[] }[] };
export type BriefsData = { briefs: Brief[] };
