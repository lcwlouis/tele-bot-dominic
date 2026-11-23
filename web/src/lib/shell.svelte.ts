import { getJSON } from "$lib/api";

/** What the navigation shows on every page: the count of things waiting for the owner. */
class Shell {
  waiting = $state(0);

  async refresh(): Promise<void> {
    try {
      this.waiting = (await getJSON<{ waiting: number }>("/api/shell")).waiting;
    } catch {
      // the pages say when Dom can't be reached; the dot just keeps its last answer
    }
  }
}

export const shell = new Shell();
