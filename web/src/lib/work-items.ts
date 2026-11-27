import type { DeskHistory, DeskTask, OwnTask } from "$lib/api";
import type { Status } from "$lib/components/kit/status";

/**
 * One strip of the Work list: a desktop task (the one on the computer now, or a finished one) or a task Dom set
 * himself. Both kinds share one newest-first list (E76); `key` keeps their IDs apart. `title` is the brief's first
 * clause and `outcome` the first line of its result (or its error), so similar briefs read apart.
 */
export type WorkItem =
  | { key: string; kind: "desk"; live: boolean; status: Status; word: string; title: string; brief: string; outcome: string; at: string; task: DeskTask | DeskHistory }
  | { key: string; kind: "own"; live: boolean; status: Status; word: string; title: string; brief: string; outcome: string; at: string; task: OwnTask };

export const deskKey = (id: number) => `task-${id}`;
export const ownKey = (id: number) => `own-${id}`;

const DESK: Record<string, Status> = {
  queued: "queued",
  running: "running",
  waiting_owner: "waiting",
  done: "done",
  failed: "failed",
  cancelled: "cancelled",
};
const OWN: Record<string, Status> = {
  planned: "queued",
  running: "running",
  done: "done",
  failed: "failed",
  cancelled: "cancelled",
  refused: "held",
  expired: "cancelled",
};
const DESK_LIVE = new Set(["queued", "running", "waiting_owner"]);
const OWN_LIVE = new Set(["planned", "running"]);

/** "waiting_owner" reads "Waiting for you"; the rest are their own word. */
const word = (status: string) => (status === "waiting_owner" ? "waiting for you" : status.replaceAll("_", " "));

/** The desk's task now (if any), its history and Dom's own tasks, newest first. */
export function workItems(current: DeskTask | null, history: DeskHistory[], own: OwnTask[]): WorkItem[] {
  const items: WorkItem[] = [];
  const seen = new Set<number>();
  for (const task of current ? [current, ...history] : history) {
    if (seen.has(task.id)) continue;
    seen.add(task.id);
    const at = (task as Partial<DeskHistory>).finished_at ?? task.created_at; // the task now has no end yet
    items.push({
      key: deskKey(task.id), kind: "desk", live: DESK_LIVE.has(task.status), status: DESK[task.status] ?? "paused",
      word: word(task.status), title: shortTitle(task.brief), brief: task.brief,
      outcome: firstLine((task as Partial<DeskHistory>).result) || firstLine(task.error), at, task,
    });
  }
  for (const task of own) {
    items.push({
      key: ownKey(task.id), kind: "own", live: OWN_LIVE.has(task.status), status: OWN[task.status] ?? "paused",
      word: word(task.status), title: shortTitle(task.goal), brief: task.goal,
      outcome: firstLine(task.result) || firstLine(task.error), at: task.finished_at || task.announced_at, task,
    });
  }
  // Newest first, by the time each ended (or began, while it runs).
  return items.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
}

/**
 * A task's short title: the brief's first clause ("Open the docs page, skim the setup section and save…" reads
 * "Open the docs page"), so a list of similar briefs reads apart. A first clause too short to say anything keeps
 * the next one; a long one is cut at a word, near 72 characters.
 */
export function shortTitle(text: string, limit = 72): string {
  const flat = text.replace(/\s+/g, " ").trim();
  let title = flat;
  for (const match of flat.matchAll(/[,;:.!?](?=\s)|\s[-–—]\s/g)) {
    const head = flat.slice(0, match.index).trim();
    if (head.length >= 16) {
      title = head;
      break;
    }
  }
  title = title.replace(/(?<!\.)\.$/, ""); // a title, not a sentence: no full stop (an ellipsis stays)
  if (title.length <= limit) return title;
  const cut = title.slice(0, limit);
  const space = cut.lastIndexOf(" ");
  return `${(space > limit / 2 ? cut.slice(0, space) : cut).replace(/[\s,;:.-]+$/, "")}…`;
}

/** The first line of what a task brought back, without markdown markers: the strip's second line. */
export function firstLine(text: string | null | undefined): string {
  for (const raw of (text ?? "").split("\n")) {
    const line = raw.replace(/^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)/, "").replace(/[*_`]{1,3}/g, "").trim();
    if (line) return line;
  }
  return "";
}
