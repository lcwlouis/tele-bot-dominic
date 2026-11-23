import type { Component } from "svelte";
import Image from "@lucide/svelte/icons/image";
import LifeBuoy from "@lucide/svelte/icons/life-buoy";
import MessageCircle from "@lucide/svelte/icons/message-circle";
import PenLine from "@lucide/svelte/icons/pen-line";
import Rocket from "@lucide/svelte/icons/rocket";
import Search from "@lucide/svelte/icons/search";
import type { Role } from "$lib/api";

/** What each model role does, in the owner's words, with the notes the old settings page gave. */
export const ROLE_INFO: Record<Role, { label: string; summary: string; note?: string; icon: Component }> = {
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
};

export const ROLES = Object.keys(ROLE_INFO) as Role[];
/** Roles that may be left unset; chat, media and drafting always have a primary. */
export const CLEARABLE: Role[] = ["fallback", "search", "escalation"];
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

export const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;
