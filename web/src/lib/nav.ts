import type { Component } from "svelte";
import Bell from "@lucide/svelte/icons/bell";
import FileText from "@lucide/svelte/icons/file-text";
import Gauge from "@lucide/svelte/icons/gauge";
import House from "@lucide/svelte/icons/house";
import Layers from "@lucide/svelte/icons/layers";
import MessageCircle from "@lucide/svelte/icons/message-circle";
import Monitor from "@lucide/svelte/icons/monitor";
import NotebookText from "@lucide/svelte/icons/notebook-text";
import Puzzle from "@lucide/svelte/icons/puzzle";
import ScrollText from "@lucide/svelte/icons/scroll-text";
import Settings from "@lucide/svelte/icons/settings";
import Sparkles from "@lucide/svelte/icons/sparkles";

/** A page inside a destination. */
export type Tab = { id: string; label: string; href: string; icon: Component; blurb: string };

export type Destination = {
  id: string;
  label: string;
  href: string;
  icon: Component;
  blurb: string;
  tabs?: Tab[];
};

/** The five destinations: the phone's menu and the desktop sidebar's "Control surface" group. */
export const destinations: Destination[] = [
  { id: "home", label: "Home", href: "/app", icon: House, blurb: "What Dom is doing and what waits for you." },
  {
    id: "chats", label: "Chats", href: "/app/chats", icon: MessageCircle,
    blurb: "Every chat Dom is in: presence, presets and personality per chat.",
  },
  {
    id: "memory", label: "Memory", href: "/app/memory", icon: NotebookText,
    blurb: "What Dom remembers, his soul file and memory imports.",
  },
  {
    id: "work", label: "Work", href: "/app/work", icon: Monitor,
    blurb: "Things Dom does for you: desktop tasks, his own work, reminders and briefs.",
    tabs: [
      { id: "desk", label: "Desk", href: "/app/work", icon: Monitor, blurb: "The desktop Dom works on, and handoffs to you." },
      { id: "own-work", label: "Own work", href: "/app/work/own-work", icon: Sparkles, blurb: "Work Dom set himself, and his skills." },
      { id: "reminders", label: "Reminders", href: "/app/work/reminders", icon: Bell, blurb: "What Dom will remind you of, and when." },
      { id: "briefs", label: "Briefs", href: "/app/work/briefs", icon: FileText, blurb: "The briefs Dom wrote for you." },
    ],
  },
  {
    id: "system", label: "System", href: "/app/system", icon: Settings,
    blurb: "How Dom runs: models and providers, plugins, spend and the turn log.",
    tabs: [
      { id: "settings", label: "Settings", href: "/app/system", icon: Settings, blurb: "Models, providers and permissions." },
      { id: "plugins", label: "Plugins", href: "/app/system/plugins", icon: Puzzle, blurb: "Plugins Dom drafted, their reviews and panels." },
      { id: "usage", label: "Usage", href: "/app/system/usage", icon: Gauge, blurb: "Spend by day, model and kind, against the daily cap." },
      { id: "turns", label: "Turn log", href: "/app/system/turns", icon: ScrollText, blurb: "Every turn: what Dom did, how long it took and which model ran." },
    ],
  },
];

/** The desktop sidebar's "Workspace" group: shortcuts to the five utility pages (each lives in Work or System as
 * a tab, which is where the phone reaches them) and the component library. */
export const workspace: { tab: Tab; home: string }[] = [
  ...["reminders", "briefs"].map((id) => ({ tab: tabOf("work", id), home: "Work" })),
  ...["plugins", "usage", "turns"].map((id) => ({ tab: tabOf("system", id), home: "System" })),
];

// Everything waiting for the owner's yes or no: reached from Home's card and Chats' pending link, not from the navigation.
export const waiting = { label: "Waiting for you", href: "/app/waiting", blurb: "Everything Dom won't do until you say so." };

// A design reference, reachable only by its address: not in the navigation.
export const gallery = { label: "Component library", href: "/app/gallery", icon: Layers };

function tabOf(destination: string, id: string): Tab {
  return destinations.find((item) => item.id === destination)!.tabs!.find((tab) => tab.id === id)!;
}

/** The destination and tab a path belongs to; unknown paths fall back to Home. */
export function locate(path: string): { destination: Destination; tab?: Tab } {
  if (path === waiting.href) return { destination: destinations[0] };
  for (const destination of destinations) {
    // A page under a tab (a plugin's review) belongs to that tab. The first tab shares its address with the destination, so only an exact match counts for it.
    const tab = destination.tabs?.find((item) => item.href === path || (item.href !== destination.href && path.startsWith(item.href + "/")));
    if (tab) return { destination, tab };
    if (destination.href === path) return { destination };
  }
  return { destination: destinations[0] };
}
