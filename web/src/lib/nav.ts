import type { Component } from "svelte";
import Bell from "@lucide/svelte/icons/bell";
import FileText from "@lucide/svelte/icons/file-text";
import FolderOpen from "@lucide/svelte/icons/folder-open";
import Gauge from "@lucide/svelte/icons/gauge";
import Globe from "@lucide/svelte/icons/globe";
import House from "@lucide/svelte/icons/house";
import Images from "@lucide/svelte/icons/images";
import KeyRound from "@lucide/svelte/icons/key-round";
import Layers from "@lucide/svelte/icons/layers";
import MessageCircle from "@lucide/svelte/icons/message-circle";
import MessagesSquare from "@lucide/svelte/icons/messages-square";
import Monitor from "@lucide/svelte/icons/monitor";
import PiggyBank from "@lucide/svelte/icons/piggy-bank";
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

/** The six destinations: the phone's menu and the desktop sidebar's "Control surface" group. */
export const destinations: Destination[] = [
  { id: "home", label: "Home", href: "/app", icon: House, blurb: "What Dom is doing and what waits for you." },
  {
    id: "talk", label: "Talk", href: "/app/talk", icon: MessagesSquare,
    blurb: "Talk to Dom here, in the same conversation as your Telegram DM, with every turn's trace a switch away.",
  },
  {
    id: "chats", label: "Chats", href: "/app/chats", icon: MessageCircle,
    blurb: "Every chat Dom is in: presence, presets and personality per chat.",
  },
  {
    id: "memory", label: "Memory", href: "/app/memory", icon: NotebookText,
    blurb: "What Dom remembers, his soul file and memory imports.",
    tabs: [
      { id: "notes", label: "Notes", href: "/app/memory", icon: NotebookText, blurb: "What Dom remembers, his soul file and memory imports." },
      { id: "world", label: "World", href: "/app/memory/world", icon: Globe, blurb: "Dom's own life: where he lives and works, his routine and the places he goes." },
    ],
  },
  {
    id: "work", label: "Work", href: "/app/work", icon: Monitor,
    blurb: "Things Dom does for you: tasks, his own work and skills, reminders, briefs and his library.",
    tabs: [
      { id: "desk", label: "Tasks", href: "/app/work", icon: Monitor, blurb: "Desktop tasks and work Dom set himself, and the computer he works on." },
      { id: "own-work", label: "Own work", href: "/app/work/own-work", icon: Sparkles, blurb: "How Dom's own work runs, and the skills he learned." },
      { id: "reminders", label: "Reminders", href: "/app/work/reminders", icon: Bell, blurb: "What Dom will remind you of, and when." },
      { id: "briefs", label: "Briefs", href: "/app/work/briefs", icon: FileText, blurb: "The briefs Dom wrote for you." },
      { id: "files", label: "Files", href: "/app/work/files", icon: FolderOpen, blurb: "The workspace Dom and his desktop share, and the files Dom may read. Browse, preview, upload and download." },
      { id: "library", label: "Library", href: "/app/work/library", icon: Images, blurb: "What Dom made and kept: photos, voice and video notes, his own files and exports." },
    ],
  },
  {
    id: "system", label: "System", href: "/app/system", icon: Settings,
    blurb: "How Dom runs: models and providers, services, plugins, spend and the turn log.",
    tabs: [
      { id: "settings", label: "Settings", href: "/app/system", icon: Settings, blurb: "Models, providers, permissions and services." },
      { id: "access", label: "Access", href: "/app/system/access", icon: KeyRound, blurb: "Saved logins, desktop permissions and approvals history." },
      { id: "plugins", label: "Plugins", href: "/app/system/plugins", icon: Puzzle, blurb: "Plugins Dom drafted, their reviews and panels." },
      { id: "spend", label: "Spend", href: "/app/system/spend", icon: PiggyBank, blurb: "Today and this month against your caps, by area, and the caps themselves." },
      { id: "usage", label: "Usage", href: "/app/system/usage", icon: Gauge, blurb: "Spend by day, model and kind, against the daily cap." },
      { id: "turns", label: "Turn log", href: "/app/system/turns", icon: ScrollText, blurb: "Every turn: what Dom did, how long it took and which model ran." },
    ],
  },
];

/** The desktop sidebar's "Workspace" group: shortcuts to the utility pages (each lives in Work or System as
 * a tab, which is where the phone reaches them) and the component library. */
export const workspace: { tab: Tab; home: string }[] = [
  ...["reminders", "briefs", "library"].map((id) => ({ tab: tabOf("work", id), home: "Work" })),
  ...["access", "plugins", "spend", "usage", "turns"].map((id) => ({ tab: tabOf("system", id), home: "System" })),
];

// Everything waiting for the owner's yes or no: reached from Home's card and Chats' pending link, not from the navigation.
export const waiting = { label: "Waiting for you", href: "/app/waiting", blurb: "Everything Dom won't do until you say so." };

// One-time secure secret entry form (E101): opened from Dom's Telegram card link or Waiting, not in navigation.
export const secretEntry = { label: "Enter secret", href: "/app/secret", blurb: "Enter a secret Dom requested securely." };

// The search box's results, reached from the top row and the phone menu: not a destination.
export const search = { label: "Search", href: "/app/search", blurb: "One search over chats, memory, briefs, reminders, tasks, skills and plugins." };

/** The search page's address for a query, with `mode=meaning` when the owner asks for meaning instead of keywords. */
export function searchHref(text: string, meaning: boolean): string {
  const query = text.trim();
  if (!query) return search.href;
  return `${search.href}?q=${encodeURIComponent(query)}${meaning ? "&mode=meaning" : ""}`;
}

// A design reference, reachable only by its address: not in the navigation.
export const gallery = { label: "Component library", href: "/app/gallery", icon: Layers };

function tabOf(destination: string, id: string): Tab {
  return destinations.find((item) => item.id === destination)!.tabs!.find((tab) => tab.id === id)!;
}

/** The destination and tab a path belongs to; unknown paths fall back to Home. */
export function locate(path: string): { destination: Destination; tab?: Tab } {
  if (path === waiting.href || path === search.href || path === secretEntry.href) return { destination: destinations[0] };
  for (const destination of destinations) {
    // A page under a tab (a plugin's review) belongs to that tab. The first tab shares its address with the destination, so only an exact match counts for it.
    const tab = destination.tabs?.find((item) => item.href === path || (item.href !== destination.href && path.startsWith(item.href + "/")));
    if (tab) return { destination, tab };
    if (destination.href === path) return { destination };
  }
  return { destination: destinations[0] };
}
