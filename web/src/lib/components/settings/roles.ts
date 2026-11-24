import type { Component } from "svelte";
import Image from "@lucide/svelte/icons/image";
import LifeBuoy from "@lucide/svelte/icons/life-buoy";
import MessageCircle from "@lucide/svelte/icons/message-circle";
import PenLine from "@lucide/svelte/icons/pen-line";
import Rocket from "@lucide/svelte/icons/rocket";
import Scale from "@lucide/svelte/icons/scale";
import Search from "@lucide/svelte/icons/search";
import type { DataPolicy, ProviderKind, Role, SettingsRole } from "$lib/api";

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
  triage: {
    label: "Triage", summary: "Decides whether and when to reply.", icon: Scale,
    note: "A decisions model (Jev): it also screens replies for leaks. Its test sends a real decision request.",
  },
};

/** The routing roles; the decision roles (triage, and later others) have their own group. */
export const ROLES: Role[] = ["chat", "fallback", "media", "drafting", "search", "escalation"];
export const DECISION_ROLES: SettingsRole[] = ["triage"];

export const KIND_INFO: Record<ProviderKind, { label: string; url: string; group: "models" | "search" }> = {
  openai: { label: "OpenAI-compatible", url: "", group: "models" },
  openrouter: { label: "OpenRouter", url: "https://openrouter.ai/api/v1", group: "models" },
  jev: { label: "Jev decisions", url: "https://openrouter.ai/api/alpha", group: "models" },
  searxng: { label: "SearXNG", url: "", group: "search" },
  deepseek: { label: "DeepSeek web search", url: "", group: "search" },
  tavily: { label: "Tavily", url: "", group: "search" },
};
export const KINDS = Object.keys(KIND_INFO) as ProviderKind[];

/** Which provider kinds can serve a role's slot. */
export function kindsFor(role: SettingsRole): ProviderKind[] {
  if (role === "triage") return ["jev"];
  if (role === "search") return ["searxng", "openrouter", "deepseek", "tavily"];
  return ["openai", "openrouter"];
}

/** Providers at one address and key are one provider unless they serve different kinds of call (model_settings.family). */
export const family = (kind: ProviderKind) => (kind === "openai" || kind === "openrouter" ? "chat" : kind);

export function useLabel(use: { role: SettingsRole; index: number }): string {
  const label = ROLE_INFO[use.role].label;
  return use.index === 0 ? label : `${label} ${slotLabel(use.index).toLowerCase()}`;
}
/** Roles that may be left unset; chat, media and drafting always have a primary. */
export const CLEARABLE: SettingsRole[] = ["fallback", "search", "escalation"];
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
