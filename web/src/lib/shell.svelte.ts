import { getJSON } from "$lib/api";

/** What the navigation shows on every page: the count of things waiting for the owner, and Dom's version. */
class Shell {
  waiting = $state(0);
  version = $state("");

  async refresh(): Promise<void> {
    try {
      const answer = await getJSON<{ waiting: number; version: string }>("/api/shell");
      this.waiting = answer.waiting;
      this.version = answer.version;
    } catch {
      // the pages say when Dom can't be reached; the dot just keeps its last answer
    }
  }
}

export const shell = new Shell();
