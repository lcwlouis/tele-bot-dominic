import type { Status } from "$lib/components/kit/status";

// Fictional data for the component library's mock Work list and Turn log. Times count back from a pinned clock,
// so "5 min ago" reads the same in every screenshot.
export const NOW = new Date("2026-11-25T18:00:00Z");
const minutes = (n: number) => new Date(NOW.getTime() - n * 60_000);

export type MockTask = { id: string; status: Status; title: string; at: Date; kind: string; steps: string[]; note?: string };

export const tasks: MockTask[] = [
  { id: "t1", status: "running", title: "Compare three standing desks under $400", at: minutes(2), kind: "Desktop task", steps: ["Opened the browser", "Searched two shops", "Reading reviews"] },
  { id: "t2", status: "waiting", title: "Sign in to the library site so I can renew the books", at: minutes(18), kind: "Handoff", steps: ["Opened the library site", "Asked you to sign in"], note: "Dom stopped at the sign-in page and waits for you." },
  { id: "t3", status: "done", title: "Summarise the book club thread from this week", at: minutes(95), kind: "Own work", steps: ["Read 42 messages", "Wrote a brief"] },
  { id: "t4", status: "failed", title: "Download the October bank statement", at: minutes(60 * 26), kind: "Desktop task", steps: ["Opened the bank site", "The page timed out twice"], note: "The bank site didn't load. Try again later." },
  { id: "t5", status: "paused", title: "Tidy the photo folder by year", at: minutes(60 * 50), kind: "Own work", steps: ["Sorted 2024", "Paused for the night"] },
  { id: "t6", status: "done", title: "Find a vegetarian recipe with leeks", at: minutes(60 * 75), kind: "Desktop task", steps: ["Found four recipes", "Saved the shortest"] },
];

/** Older tasks, as a second page would bring them. */
export function olderTasks(from: number): MockTask[] {
  const titles = ["Check the train times for Saturday", "Draft a thank-you note", "Look up the museum's late opening", "Price a replacement kettle"];
  return titles.map((title, index) => ({
    id: `old-${from + index}`,
    status: index % 3 === 1 ? "failed" : "done",
    title,
    at: minutes(60 * 24 * (4 + from + index)),
    kind: index % 2 ? "Own work" : "Desktop task",
    steps: ["Started", "Finished"],
  }));
}

export const chats = [
  { value: "", label: "All chats" },
  { value: "dm", label: "Your DM", detail: "Direct message" },
  { value: "crew", label: "Weekend crew", detail: "Group · 6 people" },
  { value: "book", label: "Book club", detail: "Group · 9 people" },
  { value: "sam", label: "Sam", detail: "Friend's DM" },
];

export type MockTurn = { id: string; chat: string; status: Status; title: string; at: Date; model: string; took: string; calls: string[] };

export const turns: MockTurn[] = [
  { id: "u1", chat: "dm", status: "running", title: "Thinking about your question on the trip budget", at: minutes(1), model: "example/chat-large", took: "4.2 s so far", calls: ["search"] },
  { id: "u2", chat: "crew", status: "done", title: "Replied in 2 bubbles", at: minutes(7), model: "example/chat-large", took: "3.1 s", calls: ["final"] },
  { id: "u3", chat: "crew", status: "held", title: "Held back: the reply mentioned your DM", at: minutes(12), model: "example/chat-large", took: "2.7 s", calls: ["leak check"] },
  { id: "u4", chat: "book", status: "done", title: "Reacted with a laugh", at: minutes(40), model: "example/chat-small", took: "0.9 s", calls: ["final"] },
  { id: "u5", chat: "sam", status: "failed", title: "The model didn't answer in time", at: minutes(64), model: "example/chat-large", took: "30 s", calls: [] },
  { id: "u6", chat: "book", status: "done", title: "Stayed quiet", at: minutes(130), model: "example/triage", took: "0.4 s", calls: [] },
];
