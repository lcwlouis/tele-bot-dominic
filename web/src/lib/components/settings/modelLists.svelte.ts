import { ApiError, send, type ListedModel, type ModelList } from "$lib/api";

/** Each provider's fetched model list, shared by every open picker. The server caches lists for a day. */
type Entry = { state: "loading" | "ready" | "error"; list: ModelList | null; error: string };

export const lists = $state<Record<string, Entry>>({});

/** Fetch a provider's list (from Dom's day-long cache unless `refresh`). Concurrent callers share one request. */
export async function loadList(name: string, revision: string, refresh = false): Promise<void> {
  const entry = lists[name];
  if (entry?.state === "loading" || (entry?.state === "ready" && !refresh)) return;
  lists[name] = { state: "loading", list: entry?.list ?? null, error: "" };
  try {
    const list = await send<ModelList>("/api/providers/models", { revision, name, refresh }, { quiet: true });
    lists[name] = { state: "ready", list, error: "" };
  } catch (error) {
    lists[name] = { state: "error", list: entry?.list ?? null, error: error instanceof ApiError ? error.message : "Couldn't fetch the list." };
  }
}

/** Forget every list, after a provider's address changes (the server's cache is keyed by address). */
export function forgetLists() {
  for (const name of Object.keys(lists)) delete lists[name];
}

export function findModel(name: string | null, id: string): ListedModel | undefined {
  return name ? lists[name]?.list?.models.find((model) => model.id === id) : undefined;
}

export const DEFAULT_EFFORTS = ["low", "medium", "high"];

/** The efforts to offer for a model: what its provider lists, none when it doesn't reason, else the usual three. */
export function effortsFor(model: ListedModel | undefined): string[] {
  if (model?.reasoning === false) return [];
  return model?.efforts.length ? model.efforts : DEFAULT_EFFORTS;
}

export function fetchedAgo(at: number): string {
  const minutes = Math.max(0, Math.round((Date.now() / 1000 - at) / 60));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  return `${Math.round(minutes / 60)} h ago`;
}
