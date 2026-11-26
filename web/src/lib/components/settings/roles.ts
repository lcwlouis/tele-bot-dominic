import type { Component } from "svelte";
import Image from "@lucide/svelte/icons/image";
import LifeBuoy from "@lucide/svelte/icons/life-buoy";
import MessageCircle from "@lucide/svelte/icons/message-circle";
import PenLine from "@lucide/svelte/icons/pen-line";
import Rocket from "@lucide/svelte/icons/rocket";
import Scale from "@lucide/svelte/icons/scale";
import ScanSearch from "@lucide/svelte/icons/scan-search";
import Search from "@lucide/svelte/icons/search";
import type { DataPolicy, ProviderKind, ProviderSetup, ProviderType, Role, SettingsRole, StudioChain } from "$lib/api";

/** What each model role does, in the owner's words, with the notes the old settings page gave. */
export const ROLE_INFO: Record<SettingsRole, { label: string; summary: string; note?: string; icon: Component }> = {
  chat: { label: "Chat", summary: "Writes replies.", icon: MessageCircle },
  fallback: {
    label: "Fallback", summary: "Takes over when chat is down.", icon: LifeBuoy,
    note: "Chat tries this role after exhausting its own providers.",
  },
  media: { label: "Photos and voice", summary: "Understands photos and voice notes.", icon: Image },
  drafting: {
    label: "Drafting", summary: "Writes skills and plugin drafts.", icon: PenLine,
    note: "Drafting tries the chat route (chat, then fallback) after its own providers.",
  },
  search: {
    label: "Web search", summary: "Looks things up online.", icon: Search,
    note: "Search tries providers in order, falling through on errors and timeouts.",
  },
  escalation: {
    label: "Escalation", summary: "A bigger model for a task you approve.", icon: Rocket,
    note: "Used only after you approve a model switch for a task. Uses the drafting providers when unset.",
  },
  embedding: {
    label: "Search by meaning", summary: "Turns notes and messages into vectors for meaning search.", icon: ScanSearch,
    note: "An embeddings model, such as one on your home lab. A new model waits for you to confirm the reindex below before it serves searches.",
  },
  triage: {
    label: "Triage", summary: "Decides whether and when to reply.", icon: Scale,
    note: "A decisions model, such as Jev: it also screens replies for leaks. Its test sends a real decision request.",
  },
};

/** The routing roles; the decision roles (triage, and later others) have their own group. */
export const ROLES: Role[] = ["chat", "fallback", "media", "drafting", "search", "escalation"];
export const DECISION_ROLES: SettingsRole[] = ["triage"];

/** Each service's name and usual address for a type (dom/config.py MEDIA_BASE_URLS; the decisions API is Jev's). */
export const KIND_INFO: Record<ProviderKind, { label: string; url: Partial<Record<ProviderType, string>> }> = {
  openai: { label: "OpenAI-compatible", url: {} },
  openrouter: {
    label: "OpenRouter",
    url: { chat: "https://openrouter.ai/api/v1", media: "https://openrouter.ai/api/v1", decisions: "https://openrouter.ai/api/alpha" },
  },
  gemini: { label: "Google Gemini", url: { media: "https://generativelanguage.googleapis.com/v1beta" } },
  searxng: { label: "SearXNG", url: {} },
  deepseek: { label: "DeepSeek web search", url: {} },
  tavily: { label: "Tavily", url: {} },
};

/** What each provider type is for, and the services that can be one (dom/config.py TYPE_KINDS). */
export const TYPE_INFO: Record<ProviderType, { label: string; heading: string; summary: string; kinds: ProviderKind[] }> = {
  chat: {
    label: "Chat", heading: "Chat providers", kinds: ["openai", "openrouter"],
    summary: "Used by chat, fallback, drafting, escalation, photos and voice, and OpenRouter web search.",
  },
  media: {
    label: "Media", heading: "Media providers", kinds: ["openrouter", "gemini"],
    summary: "Used by the studio's cloud backups for voice notes, photos and video notes.",
  },
  decisions: {
    label: "Decisions", heading: "Decision providers", kinds: ["openrouter"],
    summary: "Used by triage, through OpenRouter's Decisions API.",
  },
  search: { label: "Web search", heading: "Web search", kinds: ["searxng", "deepseek", "tavily"], summary: "Used by web search." },
};
export const TYPES = Object.keys(TYPE_INFO) as ProviderType[];

/** Whether a provider can serve a role's slot (model_settings.kind_fits): triage takes decisions providers, search
 * its own services or OpenRouter chat, the other roles chat providers. */
export function fitsRole(provider: Pick<ProviderSetup, "kind" | "type">, role: SettingsRole): boolean {
  if (role === "triage") return provider.type === "decisions";
  if (role === "search") return provider.type === "search" || (provider.type === "chat" && provider.kind === "openrouter");
  return provider.type === "chat";
}

const BACKUP_LABELS: Record<StudioChain, string> = { voice: "Voice notes", image: "Photos", video: "Video notes" };

export function useLabel(use: { role: SettingsRole | StudioChain; index: number }): string {
  if (use.role in BACKUP_LABELS) return `${BACKUP_LABELS[use.role as StudioChain]} backup ${use.index + 1}`;
  const label = ROLE_INFO[use.role as SettingsRole].label;
  return use.index === 0 ? label : `${label} ${slotLabel(use.index).toLowerCase()}`;
}
/** Roles that may be left unset; chat, media and drafting always have a primary. Without embedding, meaning search is off. */
export const CLEARABLE: SettingsRole[] = ["fallback", "search", "escalation", "embedding"];
export const MAX_SLOTS = 6; // a primary and at most five fallbacks (MAX_FALLBACKS in model_settings.py)
export const KEY_PATTERN = "[A-Z_][A-Z0-9_]{0,63}";

export const slotLabel = (index: number) => (index === 0 ? "Primary" : `Fallback ${index}`);

/** A provider's host, for display: "llm.example.org". */
export function host(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url || "No address";
  }
}

export function endpointHostname(url: string): string {
  try {
    return new URL(url.trim()).hostname.replace(/\.$/, "");
  } catch {
    return "";
  }
}

export const isDeepSeekChat = (url: string) => endpointHostname(url) === "api.deepseek.com";
/** The levels DeepSeek's own API takes (dom/config.py DEEPSEEK_EFFORTS); without one Dom keeps thinking off. */
export const DEEPSEEK_EFFORTS = ["low", "high", "max"];
export const ZDR_RECOMMENDATION = "Zero data retention is recommended.";

export const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;

/** What each OpenRouter data policy lets a studio backup's endpoints do with what Dom sends (docs/privacy.md). */
export const POLICY_INFO: Record<DataPolicy, { label: string; line: string }> = {
  zdr: { label: "Zero data retention", line: "Only endpoints that keep nothing. The default." },
  no_training: { label: "No training", line: "Endpoints may keep what's sent, but never train on it." },
  any: { label: "Any endpoint", line: "Any endpoint, including ones that may train on what's sent." },
};
export const POLICIES: DataPolicy[] = ["zdr", "no_training", "any"];
/** Shown wherever a route is set below zero data retention. */
export const ACCOUNT_ZDR_NOTE =
  "OpenRouter's own “ZDR endpoints only” privacy setting (or a guardrail on the key) still refuses non-ZDR endpoints until you turn it off there.";
