import type { MediaStanding, StudioKind } from "$lib/api";

export const KIND_LABELS: Record<StudioKind, string> = {
  voice: "Voice notes", video_note: "Video notes", photo: "Photos", special: "Special videos",
};

// Dom's own clock: the API sends each time with Dom's UTC offset, so the label reads the same on any device.
const clock = (iso: string) => iso.slice(11, 16);
const weekday = (iso: string) => new Date(iso.slice(0, 10) + "T00:00:00Z").toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" });
// Across a clock change the offset differs from now's, so it is shown: 01:15 means two different times that night.
const offset = (iso: string) => /(Z|[+-]\d\d:\d\d)$/.exec(iso)?.[1] ?? "";
export const nextLabel = (next: string, now: string) =>
  `Next media from ${next.slice(0, 10) === now.slice(0, 10) ? "" : weekday(next) + " "}${clock(next)}` +
  (offset(next) === offset(now) ? "" : ` (UTC${offset(next).replace("Z", "+00:00")})`);

/** What a chat may get now: the first reason nothing can go, or when the next can. `where` reads "in your DM". */
export function standingNote(standing: MediaStanding, where: string): string {
  const on = standing.kinds.filter((item) => item.on);
  if (standing.daily_left === 0) return "All chats have used the daily total.";
  if (standing.sending) return "Media is on its way.";
  if (!on.length) return `Media is off ${where}.`;
  if (!on.some((item) => item.left > 0)) return "Nothing left until a limit resets.";
  return standing.next_at ? `${nextLabel(standing.next_at, standing.now)}.` : "Media can go now.";
}
