import type { Component } from "svelte";
import CircleCheck from "@lucide/svelte/icons/circle-check";
import CircleDashed from "@lucide/svelte/icons/circle-dashed";
import CirclePause from "@lucide/svelte/icons/circle-pause";
import CircleX from "@lucide/svelte/icons/circle-x";
import Hourglass from "@lucide/svelte/icons/hourglass";
import ShieldHalf from "@lucide/svelte/icons/shield-half";

/** What a task, turn, message or reminder is doing. Each has a word, a glyph and a colour token. */
export type Status = "done" | "running" | "waiting" | "failed" | "held" | "paused";

export const STATUS: Record<Status, { label: string; icon: Component; tone: string }> = {
  done: { label: "Done", icon: CircleCheck, tone: "text-status-done" },
  running: { label: "Running", icon: CircleDashed, tone: "text-status-running" },
  waiting: { label: "Waiting for you", icon: Hourglass, tone: "text-status-waiting" },
  failed: { label: "Failed", icon: CircleX, tone: "text-status-failed" },
  held: { label: "Held back", icon: ShieldHalf, tone: "text-status-held" },
  paused: { label: "Paused", icon: CirclePause, tone: "text-status-paused" },
};

const units: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31_536_000],
  ["month", 2_592_000],
  ["week", 604_800],
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
];
const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "short" });

/** "now", "5 min. ago", "yesterday", "in 2 hr.": the strip's time column. */
export function ago(at: Date | string, now: Date = new Date()): string {
  const seconds = (new Date(at).getTime() - now.getTime()) / 1000;
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) return relative.format(Math.round(seconds / size), unit);
  }
  return "now";
}

const monthName = new Intl.DateTimeFormat(undefined, { month: "long" });
const monthYear = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });

/**
 * The strip list's date group for a time, in the viewer's own time zone: "Upcoming", "Today", "Yesterday",
 * "This week" (from Monday), "This month", then the month's name ("October", or "October 2025" in another year).
 */
export function dayGroup(at: Date | string, now: Date = new Date()): string {
  const when = new Date(at);
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const day = new Date(when.getFullYear(), when.getMonth(), when.getDate());
  // Whole calendar days between the two, rounded so a daylight-saving change doesn't shift a day.
  const days = Math.round((today.getTime() - day.getTime()) / 86_400_000);
  if (days < 0) return "Upcoming";
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days <= (today.getDay() + 6) % 7) return "This week"; // days since Monday
  if (when.getFullYear() === now.getFullYear() && when.getMonth() === now.getMonth()) return "This month";
  return (when.getFullYear() === now.getFullYear() ? monthName : monthYear).format(when);
}
