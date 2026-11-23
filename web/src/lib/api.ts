import type { Activity } from "$lib/components/Orbit.svelte";

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
