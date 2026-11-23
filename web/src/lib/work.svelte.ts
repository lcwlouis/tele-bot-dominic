import { getJSON, send } from "$lib/api";
import { toast } from "$lib/toast.svelte";

/**
 * One Work tab's data: loaded with a GET, replaced by the answer of every write.
 * A write answers { ok, message, ...fresh data }; `ok: false` is a refusal Dom explains (shown as an error toast).
 * A refused request (4xx) toasts its reason through send() and leaves the data as it was.
 */
export class WorkPage<T extends object> {
  data = $state<T | null>(null);
  failed = $state("");
  busy = $state(false); // one write at a time: each answer replaces the data

  constructor(readonly path: string) {}

  async load(query = ""): Promise<void> {
    try {
      this.data = await getJSON<T>(this.path + query);
      this.failed = "";
    } catch (error) {
      this.failed = error instanceof Error ? error.message : "Unknown error";
    }
  }

  async write(path: string, body: object): Promise<boolean> {
    if (this.busy) return false;
    this.busy = true;
    try {
      const { message, ok, ...fresh } = await send<{ ok: boolean; message: string } & T>(path, body);
      this.data = fresh as unknown as T;
      toast.show(ok ? "ok" : "error", message);
      return ok;
    } catch {
      return false; // the toast says why
    } finally {
      this.busy = false;
    }
  }
}
