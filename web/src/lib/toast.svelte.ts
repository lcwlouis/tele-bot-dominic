/*
  Toasts confirm what an action did ("Preset changed") or why it failed. One live region announces
  them; each one leaves after a few seconds, errors a little later.
*/

export type ToastTone = 'done' | 'error';

export interface Toast {
  id: number;
  text: string;
  tone: ToastTone;
}

let next = 1;

export const toasts = $state<Toast[]>([]);

export function dismiss(id: number): void {
  const index = toasts.findIndex((toast) => toast.id === id);
  if (index >= 0) toasts.splice(index, 1);
}

export function toast(text: string, tone: ToastTone = 'done', ms = tone === 'error' ? 8000 : 4000): number {
  const id = next++;
  toasts.push({ id, text, tone });
  if (toasts.length > 3) toasts.shift();
  if (ms > 0) setTimeout(() => dismiss(id), ms);
  return id;
}
