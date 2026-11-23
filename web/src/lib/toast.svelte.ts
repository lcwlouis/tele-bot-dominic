/** One toast at a time, shown by Toaster.svelte (mounted once in App.svelte) in a polite live region. */
export type Toast = { tone: "ok" | "error"; text: string };

class Toasts {
  current = $state<Toast | null>(null);
  #timer: ReturnType<typeof setTimeout> | undefined;

  show(tone: Toast["tone"], text: string) {
    this.current = { tone, text };
    clearTimeout(this.#timer);
    this.#timer = setTimeout(() => (this.current = null), tone === "error" ? 7000 : 4000);
  }
}

export const toast = new Toasts();
