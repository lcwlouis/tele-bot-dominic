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
