/*
  Formatting for times and money. Times arrive in the owner's timezone with an offset, so the wall clock
  is read straight from the string: the phone's own timezone never changes what Dom's day looks like.
*/

/** "21:40" from an ISO time. */
export function clock(iso: string): string {
  return iso.slice(11, 16);
}

/** "today", "tomorrow" or the date, relative to `now`. */
export function day(iso: string, now: string): string {
  const date = iso.slice(0, 10);
  if (date === now.slice(0, 10)) return 'today';
  const next = new Date(`${now.slice(0, 10)}T00:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  return date === next.toISOString().slice(0, 10) ? 'tomorrow' : date;
}

/** "just now", "3 min ago", "2 h ago", "4 days ago". */
export function ago(iso: string, now: string): string {
  const minutes = Math.round((Date.parse(now) - Date.parse(iso)) / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return `${days} ${days === 1 ? 'day' : 'days'} ago`;
}

/** "$0.31". */
export function usd(value: number): string {
  return `$${value.toFixed(2)}`;
}

/** "3 chats", "1 chat". */
export function count(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
