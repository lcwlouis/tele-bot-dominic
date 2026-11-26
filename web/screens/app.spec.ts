import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { PREVIEW_PORT as PORT } from "./preview";

/** tests/web_preview.py's NOW: the fictional Dom's clock, which the browser's is pinned to. */
const PREVIEW_NOW = new Date("2026-11-22T20:30:00Z");
const viewports = { phone: { width: 390, height: 844 }, desktop: { width: 1280, height: 900 } } as const;
const pages: { name: string; path: string; heading: string; tab?: string; click?: string; fill?: [string, string]; link?: string; menu?: boolean; phoneOnly?: boolean; desktopOnly?: boolean; folded?: boolean; soul?: string; region?: string; strip?: string; peek?: boolean; long?: boolean; all?: boolean; full?: boolean }[] = [
  // The shell on a page that shows where the utility pages live (Work's tabs, the sidebar's Workspace group).
  { name: "shell", path: "/app/work/reminders", heading: "Reminders" },
  { name: "home", path: "/app", heading: "Good evening." },
  // The desktop sidebar folded to its rail of icons; the page takes back the room.
  { name: "sidebar-folded", path: "/app/work/reminders", heading: "Reminders", folded: true, desktopOnly: true },
  // The gallery, captured as a full page: its Work list is a pane on a desktop and ten strips with See all on a phone.
  { name: "gallery", path: "/app/gallery", heading: "Component library", full: true },
  // The strip list with one strip's details open: beside the list's pane on a desktop, in a bottom sheet on a phone.
  { name: "gallery-strip-detail", path: "/app/gallery", heading: "Component library", strip: "Sign in to the library site", full: true },
  // A phone's See all: the whole Work list as its own screen, with its filter bar and count.
  { name: "gallery-see-all", path: "/app/gallery", heading: "Component library", all: true, phoneOnly: true },
  // Deep in the long Work list (its pane, or the phone's full view): a date group stuck to the top, back to top below.
  { name: "gallery-long", path: "/app/gallery", heading: "Component library", long: true },
  // The folded rail opened for a look by a resting mouse: over the page, which keeps its width.
  { name: "sidebar-peek", path: "/app/work/reminders", heading: "Reminders", folded: true, peek: true, desktopOnly: true },
  { name: "settings-models", path: "/app/system", heading: "Settings" },
  { name: "settings-providers", path: "/app/system", heading: "Settings", tab: "Providers" },
  { name: "settings-permissions", path: "/app/system", heading: "Settings", tab: "What Dom may do" },
  // Provider setup, and a role's editor with its model picker open on the provider's fetched list.
  { name: "settings-provider-editor", path: "/app/system", heading: "Settings", tab: "Providers", click: "Add provider" },
  { name: "settings-model-picker", path: "/app/system", heading: "Settings", click: "Edit Chat", fill: ["Model ID", "example/c"] },
  // Triage's editor with its picker open on OpenRouter's decision models (E71).
  { name: "settings-triage-picker", path: "/app/system", heading: "Settings", click: "Edit Triage", fill: ["Model ID", "decider"] },
  // A studio cloud backup's editor: provider, model from OpenRouter's video list, key, voice path and data policy.
  { name: "settings-studio-backup", path: "/app/system", heading: "Settings", click: "Add backup for video notes" },
  // The media limits editor on its own: what is left in the owner's DM and each chat type's limits.
  { name: "settings-media-limits", path: "/app/system", heading: "Settings", tab: "What Dom may do", region: "Media limits" },
  // Services (E81): each with its state, detail, fields (secrets only as set or not set) and Test.
  { name: "settings-services", path: "/app/system", heading: "Settings", tab: "Services" },
  // Spend (E86): today and the month against the caps, each area, 30 days by area and the cap editor.
  { name: "system-spend", path: "/app/system/spend", heading: "Spend" },
  // The Library (R9): kept media as strips, one opened with its photo, task files and exports.
  { name: "work-library", path: "/app/work/library", heading: "Library" },
  { name: "work-library-detail", path: "/app/work/library", heading: "Library", strip: "Dom on a park bench" },
  // Search by meaning: the mode switch in the field set to Meaning, and what it found.
  { name: "search-meaning", path: "/app/search?q=train&mode=meaning", heading: "Search" },
  { name: "chats", path: "/app/chats", heading: "Chats" },
  { name: "chats-personality", path: "/app/chats", heading: "Chats", click: "Personality" },
  { name: "memory", path: "/app/memory", heading: "Memory" },
  // A person's notes by name, previewed: source tags and pins as chips, not text.
  { name: "memory-person", path: "/app/memory?file=memory/people/4002.md", heading: "Memory" },
  // Every core memory, who pinned it and why.
  { name: "memory-core", path: "/app/memory", heading: "Memory", click: "Core memories" },
  // On a phone the list is its own screen and a file opens full width; on a desktop the list stays beside it.
  { name: "memory-file", path: "/app/memory", heading: "Memory", click: "MEMORY.md" },
  // A memory file rendered as markdown: headings, a list, a code span, a link and a quote.
  { name: "memory-preview", path: "/app/memory?file=SOUL.md", heading: "Memory", tab: "Preview" },
  // Work: one task list (desktop tasks and Dom's own) under the computer with a handoff waiting for the owner, then
  // a task opened from a link, how Dom's own work runs with his skills, reminders and a brief.
  { name: "work-desk", path: "/app/work", heading: "Tasks" },
  { name: "work-task", path: "/app/work?task=2", heading: "Tasks" },
  { name: "work-own", path: "/app/work/own-work", heading: "Own work" },
  { name: "work-reminders", path: "/app/work/reminders", heading: "Reminders" },
  { name: "work-briefs", path: "/app/work/briefs", heading: "Briefs" },
  // System and Waiting: plugins with their panel, a plugin's code review, spend, the turn log and what waits for the owner.
  { name: "system-plugins", path: "/app/system/plugins", heading: "Plugins" },
  { name: "system-plugin-review", path: "/app/system/plugins", heading: "Plugins", link: "Review code of trip_planner" },
  { name: "system-usage", path: "/app/system/usage", heading: "Usage" },
  { name: "system-turns", path: "/app/system/turns", heading: "Turn log" },
  // One turn's details: timing, model calls with their cache and prompt fingerprint.
  { name: "system-turn-detail", path: "/app/system/turns", heading: "Turn log", strip: "Replied in 2 bubbles" },
  { name: "waiting", path: "/app/waiting", heading: "Waiting for you" },
  // The phone's menu, open over a page: every destination with its tabs, and what waits for the owner.
  { name: "menu", path: "/app/work/briefs", heading: "Briefs", menu: true, phoneOnly: true },
  // A chat's recent messages, and the soul request on its own screen.
  { name: "chats-messages", path: "/app/chats", heading: "Chats", click: "Messages in Weekend crew" },
  // The search page: grouped hits, with another chat's message shown as a preview only.
  { name: "search", path: "/app/search?q=plan", heading: "Search" },
  { name: "memory-soul", path: "/app/memory", heading: "Memory", click: "Talk to Dom about his soul" },
  // The soul conversation with a reply and its proposed edit (the answer is canned here: the route itself runs below).
  { name: "memory-soul-chat", path: "/app/memory", heading: "Memory", click: "Talk to Dom about his soul", soul: "Would you mention board games?" },
];

/** Collect every CSP violation and every request that leaves Dom's origin. */
async function watch(page: Page) {
  const problems: string[] = [];
  page.on("console", (message) => {
    if (/Content Security Policy|Refused to/i.test(message.text())) problems.push(message.text());
  });
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.host !== `127.0.0.1:${PORT}`) problems.push(`external request: ${url.origin}`);
  });
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      console.error(`Refused to: ${event.violatedDirective} ${event.blockedURI}`);
    });
  });
  return problems;
}

/** The gallery's long Work list (the whole list, not the phone's preview). */
const workPane = (page: Page) => page.locator('[data-pane]:has([aria-label="Tasks, newest first"])');
const workCount = (page: Page) => workPane(page).locator("[data-count]");

/** Scroll the long Work list by `screens` of what scrolls it, or to its end: its pane on a desktop, the full view
 *  on a phone, or the page when it shows the whole list. */
async function scrollWork(page: Page, screens?: number) {
  await workPane(page).evaluate((pane, screens) => {
    const view = pane.closest<HTMLElement>("[role=dialog]");
    const scroller = view ?? (getComputedStyle(pane).overflowY === "auto" ? pane : null);
    if (scroller) {
      const from = view ? pane.offsetTop : 0;
      scroller.scrollTop = screens === undefined ? scroller.scrollHeight : from + screens * scroller.clientHeight;
    } else {
      // The page: to the list's end (not the page's, which would jump past the list's end unseen), or down it.
      const box = pane.getBoundingClientRect();
      scrollTo(0, scrollY + (screens === undefined ? box.bottom - innerHeight : box.top + screens * innerHeight));
    }
  }, screens);
}

/** Answer the soul conversation with a fixed reply and the seeded soul edit, so the screens change no state. */
async function cannedSoulReply(page: Page) {
  await page.route("**/api/memory/soul", async (route) => {
    const memory = await (await page.request.get("/api/memory")).json();
    const edit = memory.edits.find((item: { kind: string }) => item.kind === "soul_edit");
    await route.fulfill({
      json: {
        reply: "I'd keep the quiet evenings, but I do light up over a **board game**. Want me to say so?",
        proposal: { id: edit.id, diff: edit.diff },
        imports: memory.imports,
        edits: memory.edits,
      },
    });
  });
}

for (const scheme of ["dark", "light"] as const) {
  for (const [device, viewport] of Object.entries(viewports)) {
    test.describe(`${device} ${scheme}`, () => {
      test.use({ viewport, colorScheme: scheme });

      test("provider preset picker fills editable cloud and local fields", async ({ page }) => {
        const problems = await watch(page);
        await page.goto("/app/system");
        await page.getByRole("tab", { name: "Providers" }).click();
        await page.getByRole("button", { name: "Add provider", exact: true }).first().click();

        const picker = page.getByRole("combobox", { name: "Start from a preset" });
        await picker.focus();
        await picker.press("Enter");
        await page.getByRole("option", { name: "OpenRouter", exact: true }).click();
        await expect(page.getByLabel("Name", { exact: true })).toHaveValue("OpenRouter");
        await expect(page.getByLabel("Base URL")).toHaveValue("https://openrouter.ai/api/v1");
        await expect(page.getByLabel("Key variable")).toHaveValue("OPENROUTER_API_KEY");
        await expect(page.getByText("OpenRouter chat uses zero data retention.")).toBeVisible();

        // OpenRouter comes once per type: media has a fixed address and its own key; decisions is the alpha API.
        await picker.click();
        await page.getByRole("option", { name: "OpenRouter media", exact: true }).click();
        await expect(page.getByRole("combobox", { name: "Type" })).toHaveText("Media");
        await expect(page.getByLabel("Base URL")).toHaveValue("https://openrouter.ai/api/v1");
        await expect(page.getByLabel("Base URL")).toHaveAttribute("readonly", "");
        await expect(page.getByLabel("Key variable")).toHaveValue("OPENROUTER_MEDIA_API_KEY");
        await picker.click();
        await page.getByRole("option", { name: "OpenRouter decisions", exact: true }).click();
        await expect(page.getByRole("combobox", { name: "Type" })).toHaveText("Decisions");
        await expect(page.getByLabel("Base URL")).toHaveValue("https://openrouter.ai/api/alpha");
        await expect(page.getByText("OpenRouter decisions uses zero data retention.")).toBeVisible();

        await picker.click();
        await page.getByRole("option", { name: "Ollama", exact: true }).click();
        await expect(page.getByLabel("Base URL")).toHaveValue("http://localhost:11434/v1");
        await expect(page.getByLabel("Key variable")).toHaveValue("");
        await expect(page.getByText("OpenRouter chat uses zero data retention.")).toHaveCount(0);
        await page.getByLabel("Base URL").fill("http://ollama:11434/v1");
        await expect(page.getByLabel("Base URL")).toHaveValue("http://ollama:11434/v1");
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
        expect(problems).toEqual([]);
      });

      for (const { name, path, heading, tab, click, fill, link, menu, phoneOnly, desktopOnly, folded, soul, region, strip, peek, long, all, full } of pages) {
        if (phoneOnly && device !== "phone") continue;
        if (desktopOnly && device !== "desktop") continue;
        test(name, async ({ page }) => {
          const problems = await watch(page);
          if (folded) await page.addInitScript(() => localStorage.setItem("dom-sidebar", "collapsed"));
          // The preview's clock (tests/web_preview.py NOW): relative times and date groups hold in every baseline.
          await page.clock.setFixedTime(PREVIEW_NOW);
          if (soul) await cannedSoulReply(page);
          const response = await page.goto(path);
          expect(response?.headers()["content-security-policy"]).toContain("default-src 'self'");
          await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
          if (link) {
            await page.getByRole("link", { name: link }).click();
            await expect(page.getByRole("button", { name: "I've read this code" })).toBeVisible();
          }
          if (tab) await page.getByRole("tab", { name: tab }).click();
          if (click) await page.getByRole("button", { name: click, exact: true }).first().click();
          if (strip) await page.getByRole("button", { name: new RegExp(`^${strip}`) }).click();
          if (soul) {
            await page.getByRole("textbox", { name: "Message to Dom" }).fill(soul);
            await page.keyboard.press("Enter");
            await expect(page.getByText("Proposed change to SOUL.md")).toBeVisible();
          }
          if (fill) {
            await page.getByLabel(fill[0]).first().fill(fill[1]);
            await expect(page.getByRole("listbox").getByRole("option").first()).toBeVisible();
          }
          await page.waitForLoadState("networkidle");
          await page.evaluate(() => document.fonts.ready);
          // Nothing on any page is wider than the screen: long results wrap or scroll inside their own box.
          expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
          if (menu) {
            await page.getByRole("button", { name: /^Menu/ }).click();
            await expect(page.getByRole("dialog")).toBeVisible();
          } else if (all || long) {
            // Desktop: the list's column at the top of the screen. Phone: See all opens the full view.
            if (device === "desktop") await workPane(page).evaluate((pane) => pane.parentElement!.scrollIntoView());
            else {
              await page.getByRole("button", { name: "See all (400)" }).click();
              await expect(page.getByRole("dialog", { name: "Tasks" })).toBeVisible();
            }
            if (long) {
              // The end brings exactly one more page; then 2.2 screens down, back to top shows.
              await scrollWork(page);
              await expect(workCount(page)).toHaveText("Showing 100 of 400");
              await scrollWork(page, 2.2);
              await expect(page.getByRole("button", { name: "Back to the top of the list" })).toBeVisible();
            }
          } else if (!full) {
            // Grow the viewport to the whole page, so the desktop sidebar sits where a person sees it instead of
            // where a full-page capture would stitch it.
            const height = await page.evaluate(() => document.documentElement.scrollHeight);
            await page.setViewportSize({ width: viewport.width, height: Math.max(viewport.height, height) });
          }
          if (strip && device === "phone") await expect(page.getByRole("dialog")).toBeVisible();
          if (peek) {
            await page.locator("#sidebar-nav").hover({ position: { x: 20, y: 200 } });
            await expect(page.locator("aside[data-peek]")).toBeVisible();
          }
          if (region) {
            // One part of the page, captured on its own.
            await expect(page.getByRole("region", { name: region })).toHaveScreenshot(`${name}-${device}-${scheme}.png`);
            expect(problems).toEqual([]);
            return;
          }
          // Dom's local clock ticks each second, so it is masked.
          // A page with a list pane is captured whole rather than grown: a taller viewport would only grow the pane.
          await expect(page).toHaveScreenshot(`${name}-${device}-${scheme}.png`, { mask: [page.locator("[data-clock]")], fullPage: full });
          const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
          expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
          expect(problems).toEqual([]);
        });
      }
    });
  }
}

test.describe("interactions under the CSP", () => {
  test.use({ viewport: viewports.desktop, colorScheme: "dark" });

  test("strip list: a strip opens its details beside the list, Esc closes back to the strip, trash works", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/gallery");
    const list = page.getByRole("group", { name: "Tasks, newest first" });
    const strip = list.getByRole("button", { name: /^Sign in to the library site/ });
    await expect(strip).toHaveAccessibleName(/^Sign in to the library site so I can renew the books ?, Waiting for you/);
    await strip.focus();
    await page.keyboard.press("Enter");
    await expect(strip).toHaveAttribute("aria-expanded", "true");
    const panel = page.getByRole("region", { name: "Sign in to the library site so I can renew the books" });
    await expect(panel).toBeVisible();
    await expect(panel.getByRole("heading", { level: 2 })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(panel).toBeHidden();
    await expect(strip).toBeFocused();
    // Space opens it too; the close glyph has a tooltip and puts focus back.
    await page.keyboard.press(" ");
    await panel.getByRole("button", { name: "Close details" }).hover();
    await expect(page.locator("[data-slot=tooltip-content]").filter({ hasText: "Close details" })).toBeVisible();
    await panel.getByRole("button", { name: "Close details" }).click();
    await expect(strip).toBeFocused();
    // A trailing trash glyph removes the strip, and the total counts one fewer.
    await list.getByRole("button", { name: 'Move "Tidy the photo folder by year" to the trash' }).click();
    await expect(list.getByRole("button", { name: /^Tidy the photo folder/ })).toHaveCount(0);
    await expect(workCount(page)).toHaveText("Showing 49 of 399");
    expect(problems).toEqual([]);
  });

  test("library: an item's details say where it came from, the model, and what it cost", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/work/library");
    await expect(page.getByRole("heading", { level: 1, name: "Library" })).toBeVisible();
    const list = page.getByRole("group", { name: "Media Dom sent, newest first" });
    for (const [scene, expected] of [
      ["Dom on a park bench", ["Cloud backup · openrouter.ai", "bytedance-seed/seedream-5-5", "casual", "Cosy", "Once, after looking; the second try was sent · $0.043", "$0.086"]],
      ["Dom at the harbour at dusk", ["cost pending", "harbour", "Wistful"]],
      ["Good morning, the train is at nine", ["Local studio", "Higgs TTS", "4.2 s", "local, free"]],
    ] as const) {
      await list.getByRole("button", { name: new RegExp(scene) }).click();
      const made = page.locator("dl[aria-label='How it was made']");
      for (const text of expected) await expect(made).toContainText(text);
    }
    expect(problems).toEqual([]);
  });

  test("strip list: date groups head the strips and stick to the top as they scroll", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/gallery");
    const list = page.getByRole("group", { name: "Tasks, newest first" });
    await expect(list.getByRole("heading", { level: 4 })).toHaveText(["Today", "Yesterday", "This week", "This month"]);
    // Each day's strips are a list named by its heading.
    await expect(list.getByRole("list", { name: "Today" }).getByRole("listitem")).toHaveCount(3);
    await expect(list.getByRole("list", { name: "Yesterday" }).getByRole("listitem")).toHaveCount(1);
    // Scrolled into "This month", its heading sits at the top of the pane with strips passing under it.
    const month = list.getByRole("heading", { name: "This month" });
    const pane = workPane(page);
    await pane.scrollIntoViewIfNeeded();
    await scrollWork(page, 1);
    await expect.poll(async () => Math.abs((await month.boundingBox())!.y - (await pane.boundingBox())!.y)).toBeLessThanOrEqual(1);
    // Older pages bring the months by name.
    await expect
      .poll(async () => {
        await scrollWork(page);
        return list.getByRole("heading", { name: "October" }).count();
      })
      .toBe(1);
    expect(problems).toEqual([]);
  });

  test("strip list: the filter bar narrows by text and status, Esc clears it before closing details", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/gallery");
    const list = page.getByRole("group", { name: "Tasks, newest first" });
    const count = workCount(page);
    await expect(count).toHaveText("Showing 50 of 400");
    const strips = list.locator("[data-strip]");
    /** Every strip shown matches, and the count says how many of those loaded. (A narrow filter leaves the end of
     *  the list in view, so further pages load as they would on a scroll.) */
    async function onlyMatches(match: RegExp) {
      await expect(count).toHaveText(/^\d+ of \d+ loaded match$/);
      // Read the count and the strips together: further pages may still be arriving.
      await expect
        .poll(() =>
          page.evaluate((source) => {
            const pane = document.querySelector('[data-pane]:has([aria-label="Tasks, newest first"])')!;
            const names = [...pane.querySelectorAll("[data-strip]")].map((node) => node.textContent ?? "");
            const said = Number(pane.querySelector("[data-count]")!.textContent!.split(" ")[0]);
            return said === names.length && names.length > 0 && names.every((name) => new RegExp(source).test(name));
          }, match.source),
        )
        .toBe(true);
    }
    // Open a strip, then filter: Esc in the filter empties it and leaves the details open.
    await list.getByRole("button", { name: /^Compare three standing desks/ }).click();
    const filter = page.getByRole("searchbox", { name: "Filter tasks" });
    await filter.fill("kettle");
    await expect(strips.first()).toHaveText(/kettle/);
    await onlyMatches(/kettle/);
    await filter.press("Escape");
    await expect(filter).toHaveValue("");
    await expect(page.getByRole("region", { name: "Compare three standing desks under $400" })).toBeVisible();
    await expect(count).toHaveText(/^Showing \d+ of 400$/);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("region", { name: "Compare three standing desks under $400" })).toBeHidden();
    // Status chips are toggles, and more than one can be on.
    const chips = page.getByRole("group", { name: "Status" }).first();
    await chips.getByRole("button", { name: "Failed" }).click();
    await expect(chips.getByRole("button", { name: "Failed" })).toHaveAttribute("aria-pressed", "true");
    await onlyMatches(/, Failed/);
    await chips.getByRole("button", { name: "Paused" }).click();
    await expect(list.getByRole("button", { name: /, Paused/ }).first()).toBeAttached();
    await onlyMatches(/, (Failed|Paused)/);
    await filter.fill("nothing like this");
    await expect(page.getByText("Nothing loaded matches this filter.")).toBeVisible();

    // The Turn log hands its filter to the caller, which searches on the "server"; the chat picker sits in its bar.
    const turns = page.getByRole("group", { name: "Turns, newest first" });
    await page.getByRole("searchbox", { name: "Search turns" }).fill("laugh");
    await expect(turns.getByRole("listitem")).toHaveCount(1);
    await expect(page.locator('[data-pane]:has([aria-label="Turns, newest first"]) [data-count]')).toHaveText("1 loaded");
    await page.getByRole("searchbox", { name: "Search turns" }).fill("");
    await page.getByRole("group", { name: "Status" }).last().getByRole("button", { name: "Held back" }).click();
    await expect(turns.getByRole("listitem")).toHaveCount(1);
    expect(problems).toEqual([]);
  });

  test("strip list: back to top appears two screens down, returns to the start and focuses it", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/gallery");
    const top = page.getByRole("button", { name: "Back to the top of the list" });
    const pane = workPane(page);
    await pane.scrollIntoViewIfNeeded();
    await expect(top).toHaveCount(0);
    await scrollWork(page);
    await expect(workCount(page)).toHaveText("Showing 100 of 400");
    await scrollWork(page, 2.5);
    await expect(top).toBeVisible();
    await top.click();
    const first = page.getByRole("group", { name: "Tasks, newest first" }).getByRole("button", { name: /^Compare three standing desks/ });
    await expect(first).toBeFocused();
    await expect.poll(() => pane.evaluate((node) => node.scrollTop)).toBe(0);
    await expect(top).toHaveCount(0);
    expect(problems).toEqual([]);
  });

  test("strip list: past 200 strips, off-screen ones skip rendering while the open strip and focus survive", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/gallery");
    const list = page.getByRole("group", { name: "Tasks, newest first" });
    const strip = list.getByRole("button", { name: /^Sign in to the library site/ });
    await strip.click();
    const panel = page.getByRole("region", { name: "Sign in to the library site so I can renew the books" });
    await expect(panel).toBeVisible();
    // Scroll on until every page is in: 400 strips.
    const count = workCount(page);
    await expect
      .poll(
        async () => {
          await scrollWork(page);
          return count.textContent();
        },
        { timeout: 20_000 },
      )
      .toBe("Showing 400 of 400");
    await expect(page.getByRole("button", { name: "Load more" })).toHaveCount(0);
    // Off screen, a strip is skipped by layout and paint but stays in the page; the open one is still open.
    expect(await list.locator("li").first().evaluate((node) => getComputedStyle(node).contentVisibility)).toBe("auto");
    await expect(strip).toHaveAttribute("aria-expanded", "true");
    await expect(panel).toBeVisible();
    // The sticky panel followed the scroll; Esc closes it and focus returns to the strip far above.
    await panel.getByRole("heading", { level: 2 }).focus();
    await page.keyboard.press("Escape");
    await expect(strip).toBeFocused();
    await expect.poll(async () => (await strip.boundingBox())!.y).toBeGreaterThanOrEqual(0);
    expect(problems).toEqual([]);
  });

  test("strip list: a failing page stops the loading and offers to try again", async ({ page }) => {
    const problems = await watch(page);
    const failures: string[] = [];
    page.on("console", (message) => message.text().startsWith("Loading more failed") && failures.push(message.text()));
    await page.goto("/app/gallery");
    await page.getByRole("switch", { name: "Pages fail" }).click();
    await scrollWork(page);
    const retry = page.getByRole("button", { name: "Couldn't load more. Try again" });
    await expect(retry).toBeVisible();
    // The end stays in view, yet the list asked once and stopped.
    await page.waitForTimeout(1000);
    expect(failures).toHaveLength(1);
    await page.getByRole("switch", { name: "Pages fail" }).click();
    await retry.click();
    await expect(workCount(page)).toHaveText(/^Showing (100|150) of 400$/);
    expect(problems).toEqual([]);
  });

  test("strip list on a desktop: the long list is a pane that fills the screen and scrolls inside, keys included", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/gallery");
    const pane = workPane(page);
    await pane.scrollIntoViewIfNeeded();
    const height = await page.evaluate(() => document.documentElement.scrollHeight);
    // The pane fits the screen and scrolls itself; loading more pages never makes the page longer.
    expect(await pane.evaluate((node) => getComputedStyle(node).overflowY)).toBe("auto");
    const box = (await pane.boundingBox())!;
    expect(box.height).toBeGreaterThan(700);
    expect(box.height).toBeLessThanOrEqual(900);
    await scrollWork(page);
    await expect(workCount(page)).toHaveText("Showing 100 of 400");
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBe(height);
    // Keys scroll the pane when focus is inside it, and leave the page where it is.
    await scrollWork(page, 0);
    const first = page.getByRole("group", { name: "Tasks, newest first" }).getByRole("button", { name: /^Compare three standing desks/ });
    await first.focus();
    const pageY = await page.evaluate(() => scrollY);
    await page.keyboard.press("PageDown");
    await expect.poll(() => pane.evaluate((node) => node.scrollTop)).toBeGreaterThan(300);
    // End reaches the end (where the next page then starts loading).
    const end = await pane.evaluate((node) => node.scrollHeight - node.clientHeight);
    await page.keyboard.press("End");
    await expect.poll(() => pane.evaluate((node) => node.scrollTop)).toBeGreaterThanOrEqual(end - 2);
    await page.keyboard.press("Home");
    await expect.poll(() => pane.evaluate((node) => node.scrollTop)).toBe(0);
    expect(await page.evaluate(() => scrollY)).toBe(pageY);
    // The details beside it fill the same height and scroll on their own.
    await first.click();
    const panel = page.getByRole("region", { name: "Compare three standing desks under $400" });
    await expect(panel).toBeVisible();
    // (The narrower list may wrap its filter bar; the pane then shortens to keep both on the screen.)
    const bottom = async (node: typeof pane) => Math.round(await node.evaluate((element) => element.getBoundingClientRect().bottom));
    await expect.poll(async () => (await bottom(panel)) - (await bottom(pane))).toBe(0);
    // Filter bar and pane together still fit the screen, less a margin.
    expect(await pane.evaluate((element) => Math.round(element.parentElement!.getBoundingClientRect().height))).toBeLessThanOrEqual(900 - 32);
    // A short list (the Turn log) opts out and stays page length.
    expect(await page.locator('[data-pane]:has([aria-label="Turns, newest first"])').evaluate((node) => getComputedStyle(node).overflowY)).toBe("visible");
    expect(problems).toEqual([]);
  });

  test("strip list on a phone: ten strips and See all, so what is below stays in reach", async ({ page }) => {
    await page.setViewportSize(viewports.phone);
    const problems = await watch(page);
    await page.goto("/app/gallery");
    const preview = page.locator("[data-preview]");
    await expect(preview.locator("[data-strip]")).toHaveCount(10);
    // The next section starts within a screen of the list's start: no scrolling through the list to reach it.
    const [listTop, nextTop] = [(await preview.boundingBox())!.y, (await page.getByRole("heading", { name: "Mock Turn log" }).boundingBox())!.y];
    expect(nextTop - listTop).toBeLessThan(viewports.phone.height);
    // A strip in the preview opens the bottom sheet, as anywhere on a phone.
    await preview.getByRole("button", { name: /^Sign in to the library site/ }).click();
    const sheet = page.getByRole("dialog", { name: "Sign in to the library site so I can renew the books" });
    await expect(sheet).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();

    // See all: the whole list as its own screen, the page under it left where it was.
    const seeAll = page.getByRole("button", { name: "See all (400)" });
    await seeAll.hover();
    const pageY = await page.evaluate(() => scrollY);
    expect(pageY).toBeGreaterThan(0);
    await seeAll.click();
    const view = page.getByRole("dialog", { name: "Tasks" });
    await expect(view).toBeVisible();
    await expect(view.getByRole("heading", { name: "Tasks" })).toBeFocused();
    await expect(workCount(page)).toHaveText("Showing 50 of 400");
    await expect(view.getByRole("searchbox", { name: "Filter tasks" })).toBeVisible();
    // It scrolls as a page would: loading, group headers under its header, back to top.
    await scrollWork(page);
    await expect(workCount(page)).toHaveText("Showing 100 of 400");
    const header = (await view.getByRole("button", { name: "Back", exact: true }).boundingBox())!;
    await scrollWork(page, 1);
    const month = view.getByRole("heading", { name: "This month" });
    await expect.poll(async () => Math.round((await month.boundingBox())!.y)).toBe(56);
    expect(header.y).toBeLessThan(56);
    // A strip here opens the bottom sheet over the full view; Esc closes it back to that strip.
    const strip = view.getByRole("button", { name: /^Book a table for four/ }).first();
    await strip.click();
    await expect(page.getByRole("dialog", { name: "Book a table for four on Friday" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog", { name: "Book a table for four on Friday" })).toBeHidden();
    await expect(view).toBeVisible();
    await expect(strip).toBeFocused();
    await scrollWork(page, 2.5);
    const top = view.getByRole("button", { name: "Back to the top of the list" });
    await expect(top).toBeVisible();
    await top.click();
    await expect.poll(() => view.evaluate((node) => node.scrollTop)).toBe(0);
    await expect(view.getByRole("button", { name: /^Compare three standing desks/ })).toBeFocused();
    // Back returns to the page where it was, with focus on See all; so does the back gesture.
    await view.getByRole("button", { name: "Back", exact: true }).click();
    await expect(view).toBeHidden();
    await expect(page).toHaveURL(/\/app\/gallery$/);
    await expect(seeAll).toBeFocused();
    expect(await page.evaluate(() => scrollY)).toBe(pageY);
    await seeAll.click();
    await expect(view).toBeVisible();
    await page.goBack();
    await expect(view).toBeHidden();
    await expect(page).toHaveURL(/\/app\/gallery$/);
    await expect(page.getByRole("heading", { level: 1, name: "Component library" })).toBeVisible();
    expect(problems).toEqual([]);
  });

  test("strip list on a phone: a page that is only the list shows it whole, and the page scrolls", async ({ page }) => {
    await page.setViewportSize(viewports.phone);
    const problems = await watch(page);
    await page.goto("/app/gallery");
    await page.getByRole("switch", { name: "Whole list on the page" }).click();
    await expect(page.locator("[data-preview]")).toHaveCount(0);
    expect(await workPane(page).evaluate((node) => getComputedStyle(node).overflowY)).toBe("visible");
    const height = await page.evaluate(() => document.documentElement.scrollHeight);
    await scrollWork(page);
    await expect(workCount(page)).toHaveText("Showing 100 of 400");
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeGreaterThan(height);
    // Group headers stick to the top of the screen; back to top returns to the list's start.
    await scrollWork(page, 2.5);
    await expect.poll(async () => Math.round((await page.getByRole("heading", { name: "This month" }).boundingBox())!.y)).toBe(0);
    await page.getByRole("button", { name: "Back to the top of the list" }).click();
    await expect(page.getByRole("group", { name: "Tasks, newest first" }).getByRole("button", { name: /^Compare three standing desks/ })).toBeFocused();
    expect(problems).toEqual([]);
  });

  test("strip list on a phone: details open in a bottom sheet that closes on Esc, back to the strip", async ({ page }) => {
    await page.setViewportSize(viewports.phone);
    const problems = await watch(page);
    await page.goto("/app/gallery");
    const strip = page.getByRole("group", { name: "Turns, newest first" }).getByRole("button", { name: /^Held back: the reply mentioned/ });
    await strip.click();
    const sheet = page.getByRole("dialog", { name: "Held back: the reply mentioned your DM" });
    await expect(sheet).toBeVisible();
    await expect(sheet.getByText("example/chat-large")).toBeVisible();
    // It slides up to sit on the bottom edge.
    await expect.poll(async () => Math.round(await sheet.evaluate((node) => node.getBoundingClientRect().bottom))).toBe(viewports.phone.height);
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
    await expect(strip).toBeFocused();
    expect(problems).toEqual([]);
  });

  test("icon buttons: the tooltip shows on focus without repeating the name, and a click still closes it", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/gallery");
    const edit = page.getByRole("button", { name: "Edit reminder" });
    await edit.focus();
    const tip = page.locator("[data-slot=tooltip-content]").filter({ hasText: "Edit reminder" });
    await expect(tip).toBeVisible();
    // The tooltip only repeats the name, so it is not also the description.
    await expect(edit).not.toHaveAttribute("aria-describedby");
    // The tooltip's own click handler still runs beside the button's: a click closes it.
    await edit.hover();
    await expect(tip).toBeVisible();
    await edit.click();
    await expect(tip).toBeHidden();
    // The sidebar's logo button: its tooltip adds only the key, so it isn't a description either, and its click
    // closes the "Collapse" tooltip as it folds.
    const fold = page.getByRole("button", { name: "Collapse sidebar" });
    await fold.hover();
    await expect(page.locator("[data-slot=tooltip-content]").filter({ hasText: "Collapse sidebar" })).toBeVisible();
    await expect(fold).not.toHaveAttribute("aria-describedby");
    await fold.click();
    await expect(page.locator("[data-slot=tooltip-content]").filter({ hasText: "Collapse sidebar" })).toHaveCount(0);
    await page.mouse.move(900, 600);
    await page.getByRole("button", { name: "Expand sidebar" }).click();
    expect(problems).toEqual([]);
  });

  test("info glyph: a click pins it, a click elsewhere or Esc closes it", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/gallery");
    const info = page.getByRole("button", { name: "About the leak check" });
    const text = page.getByText("Before a reply goes out, a second model reads it").first();
    await info.click();
    await expect(info).toHaveAttribute("aria-expanded", "true");
    await expect(text).toBeVisible();
    await page.mouse.move(5, 890);
    await expect(text).toBeVisible();
    await info.click();
    await expect(info).toHaveAttribute("aria-expanded", "false");
    // Pinned, a click anywhere else closes it; so does Esc.
    await info.click();
    await page.getByRole("heading", { name: "Component library" }).click();
    await expect(info).toHaveAttribute("aria-expanded", "false");
    await expect(text).toBeHidden();
    await info.click();
    await expect(text).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(info).toHaveAttribute("aria-expanded", "false");
    await expect(text).toBeHidden();
    expect(problems).toEqual([]);
  });

  test("picker: filters, Esc restores the chosen label, and picking the chosen option keeps it", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/gallery");
    const picker = page.getByRole("combobox", { name: "Chat" });
    await expect(picker).toHaveValue("All chats");
    await picker.click();
    await picker.fill("book");
    await expect(page.getByRole("option")).toHaveCount(1);
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(picker).toHaveValue("Book club");
    const turns = page.getByRole("group", { name: "Turns, newest first" });
    await expect(turns.getByRole("listitem")).toHaveCount(2);
    // A half-typed filter left with Esc goes back to the chosen label.
    await picker.click();
    await picker.fill("we");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("listbox")).toBeHidden();
    await expect(picker).toHaveValue("Book club");
    // Picking the chosen option again keeps it.
    await picker.click();
    await page.getByRole("option", { name: /Book club/ }).click();
    await expect(picker).toHaveValue("Book club");
    await expect(turns.getByRole("listitem")).toHaveCount(2);
    await picker.click();
    await page.getByRole("option", { name: /All chats/ }).click();
    await expect(turns.getByRole("listitem")).toHaveCount(6);
    expect(problems).toEqual([]);
  });

  test("Home shows the active provider budget notice", async ({ page }) => {
    const line = "OpenRouter's workspace budget ($0.50) is used up. Resets 25 Nov at 08:00 (Asia/Singapore).";
    await page.route("**/api/home", async (route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...await response.json(), provider_budget: line } });
    });
    await page.goto("/app");
    await expect(page.getByText("Provider budget reached", { exact: true })).toBeVisible();
    await expect(page.getByText(line, { exact: true })).toBeVisible();
  });

  test("DeepSeek preset recommends ZDR and suggests a model for a chat role", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/system");
    await page.getByRole("tab", { name: "Providers" }).click();
    await page.getByRole("button", { name: "Add provider", exact: true }).first().click();
    await page.getByRole("combobox", { name: "Start from a preset" }).click();
    await page.getByRole("option", { name: "DeepSeek", exact: true }).click();
    await expect(page.getByLabel("Base URL")).toHaveValue("https://api.deepseek.com");
    await expect(page.getByLabel("Key variable")).toHaveValue("DEEPSEEK_API_KEY");
    await expect(page.getByText(/Selecting this provider for a model role starts with deepseek-flash/)).toBeVisible();
    await expect(page.getByText(/DeepSeek keeps inputs in China.*Zero data retention is recommended/)).toBeVisible();
    await page.getByRole("button", { name: "Add provider", exact: true }).last().click();
    await expect(page.getByRole("status").filter({ hasText: "Provider saved." })).toBeVisible();
    await page.getByRole("tab", { name: "Models" }).click();
    await page.getByRole("button", { name: "Edit Fallback" }).click();
    await page.getByRole("combobox", { name: "Provider", exact: true }).first().click();
    await page.getByRole("option", { name: "DeepSeek · OpenAI-compatible", exact: true }).click();
    await expect(page.getByLabel("Model ID").first()).toHaveValue("deepseek-flash");
    // Thinking stays off unless a DeepSeek level is chosen: low, high or max.
    await page.getByRole("combobox", { name: "Reasoning effort" }).first().click();
    await expect(page.getByRole("option")).toHaveText(["Thinking off (default)", "Low", "High", "Max"]);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("tabpanel", { name: "Models" }).getByText(
      /DeepSeek keeps inputs in China.*Zero data retention is recommended/,
    )).toBeVisible();
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await page.getByRole("tab", { name: "Providers" }).click();
    await page.getByRole("button", { name: "Remove DeepSeek", exact: true }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Remove" }).click();
    await expect(page.getByRole("status").filter({ hasText: "DeepSeek removed." })).toBeVisible();
    expect(problems).toEqual([]);
  });

  test("select, dialog and tooltip open without CSP violations", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/gallery");
    const provider = page.getByRole("combobox", { name: "Provider" });
    await provider.click();
    await page.getByRole("option", { name: "OpenRouter" }).click();
    await expect(provider).toHaveText(/OpenRouter/);
    await page.getByRole("button", { name: "Delete reminders…" }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toBeHidden();
    const trigger = page.getByRole("button", { name: "Hover for a tooltip" });
    await trigger.hover();
    const tip = page.getByText("Tooltips position themselves under the CSP.");
    await expect(tip).toBeVisible();
    // Floating UI places it with CSSOM styles, which the CSP allows: it must sit right above its trigger.
    const [tipBox, triggerBox] = [await tip.boundingBox(), await trigger.boundingBox()];
    expect(Math.abs(tipBox!.y + tipBox!.height - triggerBox!.y)).toBeLessThan(24);
    expect(problems).toEqual([]);
  });

  test("settings: a switch saves at once, the leak check asks first, a provider test answers", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/system");
    await page.getByRole("tab", { name: "What Dom may do" }).click();
    const groups = page.getByRole("switch", { name: "Private replies in groups" });
    await expect(groups).toHaveAttribute("aria-checked", "false");
    await groups.click();
    await expect(groups).toHaveAttribute("aria-checked", "true");
    await expect(page.getByRole("status").filter({ hasText: "Private replies in groups: on." })).toBeVisible();
    await page.reload();
    await page.getByRole("tab", { name: "What Dom may do" }).click();
    await expect(groups).toHaveAttribute("aria-checked", "true");
    await groups.click();
    await expect(page.getByRole("status").filter({ hasText: "Private replies in groups: off." })).toBeVisible();

    // The leak check: turning it on is one click; turning it off waits for the confirm dialog.
    const leaks = page.getByRole("switch", { name: "Check replies for leaks" });
    await leaks.click();
    await expect(page.getByRole("status").filter({ hasText: "Check replies for leaks: on." })).toBeVisible();
    await leaks.click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByRole("button", { name: "Keep it on" }).click();
    await expect(leaks).toHaveAttribute("aria-checked", "true");
    await leaks.click();
    await dialog.getByRole("button", { name: "Turn off" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Check replies for leaks: off." })).toBeVisible();
    await expect(leaks).toHaveAttribute("aria-checked", "false");

    // The desktop step budget: out of range is refused before saving; a valid one saves and moves the revision.
    const steps = page.getByLabel("Step budget");
    await steps.fill("500");
    await expect(page.getByText("Enter a whole number from 10 to 100.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Save", exact: true })).toBeDisabled();
    await steps.fill("40");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByRole("status").filter({ hasText: "Desktop tasks now start with 40 steps." })).toBeVisible();
    await leaks.click();  // the next save uses the revision that one returned
    await expect(page.getByRole("status").filter({ hasText: "Check replies for leaks: on." })).toBeVisible();

    // A studio switch nothing can serve stays off and says what to set up; the profile photo needs the local studio.
    await expect(page.getByRole("switch", { name: "Match the Everywhere preset’s profile photo" })).toBeDisabled();
    await expect(page.getByText("Set up the local studio first", { exact: false })).toBeVisible();
    // The preview's video backup has no key value yet, so it can't serve the switch either.
    await expect(page.getByRole("switch", { name: "Send round video notes" })).toBeDisabled();
    await expect(page.getByText("Set up the studio or a video backup first.").first()).toBeVisible();
    await page.getByLabel("Change at most every").click();
    await page.getByRole("option", { name: "Month" }).click();
    await expect(page.getByRole("status").filter({ hasText: "changes at most every month" })).toBeVisible();

    await page.getByRole("tab", { name: "Models" }).click();
    await page.getByRole("button", { name: "Test Chat", exact: true }).click();
    await expect(page.getByText(/OK · \d+\.\d s/).first()).toBeVisible();
    // Every saved slot has its own Test, fallbacks included.
    await page.getByRole("button", { name: "Test Drafting fallback 1" }).click();
    await expect(page.getByText(/OK · \d+\.\d s/)).toHaveCount(2);

    // The editor opens on the saved chain and cancels without writing.
    await page.getByRole("button", { name: "Edit Drafting" }).click();
    await expect(page.getByLabel("Model ID").first()).toHaveValue("example/draft-model");
    await expect(page.getByText("drafts.example.net · EXAMPLE_DRAFTS_KEY")).toBeVisible();
    await page.getByRole("button", { name: "Cancel" }).click();
    await expect(page.getByRole("button", { name: "Edit Drafting" })).toBeEnabled();
    expect(problems).toEqual([]);
  });

  test("settings: media limits show what's left, save at once and refuse a bad number", async ({ page }) => {
    const problems = await watch(page);
    // Another client saves after the page loaded but before the limits load: the editor saves with the revision that
    // came with its limits, not the page's older one.
    let release = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/studio/limits", async (route) => {
      if (route.request().method() === "GET") await held;
      await route.continue();
    });
    await page.goto("/app/system");
    await expect(page.getByRole("tab", { name: "Models" })).toBeVisible();
    const { revision } = await (await page.request.get("/api/permissions")).json();
    const interval = (await (await page.request.get("/api/permissions")).json()).profile_photo_every_days === 7 ? 14 : 7;
    const elsewhere = await page.request.post("/api/permissions/profile-interval", {
      headers: { Origin: `http://127.0.0.1:${PORT}` }, data: { revision, days: interval },
    });
    expect(elsewhere.status()).toBe(200);
    release();
    await page.getByRole("tab", { name: "What Dom may do" }).click();
    const limits = page.getByRole("region", { name: "Media limits" });
    // The preview sent a photo 14 minutes ago, inside the owner DM's 30-minute gap. Every studio switch is off there,
    // so the strip says so instead of when the gap ends.
    await expect(limits.getByRole("region", { name: "Left now in your DM" }).getByRole("listitem").filter({ hasText: "Photos" })).toHaveText(/Photos\s*1 \(off\)/);
    // Global limits sit with Your DM: the daily total, what is left of it, and cloud media spend against its limit.
    const global = limits.getByRole("group", { name: "Global limits" });
    await expect(global.getByRole("spinbutton", { name: "All chats" })).toBeVisible();
    await expect(global.getByText(/Cloud media this month: \$\d+\.\d\d of \$5\.00/)).toBeVisible();
    await expect(limits.getByText("Media is off in your DM.")).toBeVisible();
    // Special videos are for the owner's DM only.
    await expect(limits.getByLabel("Special videos")).toHaveCount(1);
    // A gap for one kind sits behind a disclosure; the event days list below the limits.
    await limits.getByText("Gaps for one kind").first().click();
    await expect(limits.getByLabel("Voice gap").first()).toHaveValue("");
    await expect(page.getByRole("region", { name: "Event days" }).getByText("None yet.")).toBeVisible();
    const save = limits.getByRole("button", { name: "Save media limits" });
    await expect(save).toBeDisabled();

    const gap = limits.getByRole("spinbutton", { name: "Gap" }).first();
    await gap.fill("10081");
    await expect(limits.getByText("The gap is at most 10080 minutes", { exact: false })).toBeVisible();
    await expect(save).toBeDisabled();
    await gap.fill("0");
    await limits.getByRole("combobox", { name: "Photos: per day or per week" }).first().click();
    await page.getByRole("option", { name: "per week" }).click();
    await save.click();
    await expect(page.getByRole("status").filter({ hasText: "Media limits saved." })).toBeVisible();
    await expect(limits.getByText("Media is off in your DM.")).toBeVisible();  // never "can go now" with nothing on
    await page.reload();
    await page.getByRole("tab", { name: "What Dom may do" }).click();
    await expect(gap).toHaveValue("0");
    await expect(limits.getByRole("combobox", { name: "Photos: per day or per week" }).first()).toHaveText("per week");

    // Another save on the page moves the revision; the limits load again and still save after it. A half-typed
    // (empty) field survives that reload.
    await gap.fill("");
    const groups = page.getByRole("switch", { name: "Private replies in groups" });
    await groups.click();
    await expect(page.getByRole("status").filter({ hasText: "Private replies in groups: on." })).toBeVisible();
    await expect(gap).toHaveValue("");
    // Back as it was.
    await gap.fill("30");
    await limits.getByRole("combobox", { name: "Photos: per day or per week" }).first().click();
    await page.getByRole("option", { name: "per day" }).click();
    await save.click();
    await expect(page.getByRole("status").filter({ hasText: "Media limits saved." })).toBeVisible();
    await groups.click();
    await expect(page.getByRole("status").filter({ hasText: "Private replies in groups: off." })).toBeVisible();
    expect(problems).toEqual([]);
  });

  test("settings: providers are added, tested and removed; a role picks a model from the fetched list", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/system");
    await page.getByRole("tab", { name: "Providers" }).click();

    // Add: the save tests the provider by fetching its model list first.
    await page.getByRole("button", { name: "Add provider" }).click();
    await expect(page.locator("#provider-add-wait")).toHaveText("Finish editing the new provider first.");
    await page.getByLabel("Name", { exact: true }).fill("Attic");
    await page.getByLabel("Base URL").fill("http://attic:8000/v1");
    await page.getByRole("button", { name: "Add provider" }).last().click();
    await expect(page.getByRole("status").filter({ hasText: "Provider saved." })).toBeVisible();
    const attic = page.locator("[data-slot=card]").filter({ hasText: "Attic" });
    await expect(attic.getByText("Nothing yet")).toBeVisible();
    await attic.getByRole("button", { name: "Test Attic" }).click();
    await expect(attic.getByText("Lists 6 models")).toBeVisible();

    // A provider a role uses can't be removed: the dialog says which roles to change first.
    await page.getByRole("button", { name: "Edit homelab" }).first().click();
    await page.getByLabel("Name", { exact: true }).fill("Home lab");
    await page.getByRole("button", { name: "Save provider" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Provider saved." }).last()).toBeVisible();
    await page.getByRole("button", { name: "Remove Home lab" }).click();
    await expect(page.getByRole("alertdialog").getByText("Home lab is still in use")).toBeVisible();
    await page.getByRole("alertdialog").getByRole("button", { name: "OK" }).click();
    // An unused one goes after a confirm.
    await page.getByRole("button", { name: "Remove Spare box" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Remove" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Spare box removed." })).toBeVisible();
    await expect(page.locator("[data-slot=card]").filter({ hasText: "Spare box" })).toHaveCount(0);

    // Models: the picker searches the provider's list; a reasoning model offers its own effort levels.
    await page.getByRole("tab", { name: "Models" }).click();
    await page.getByRole("button", { name: "Edit Fallback" }).click();
    const model = page.getByLabel("Model ID").first();
    await model.fill("large");
    const options = page.getByRole("listbox").getByRole("option");
    await expect(options).toHaveCount(1);
    await model.press("ArrowDown");
    await model.press("Enter");
    await expect(model).toHaveValue("example/chat-large");
    await page.getByLabel("Reasoning effort").first().click();
    await expect(page.getByRole("option", { name: "Xhigh" })).toBeVisible();
    await page.getByRole("option", { name: "High", exact: true }).click();
    await expect(page.getByText("Higher effort takes longer and costs more.").first()).toBeVisible();
    // Any ID can still be typed by hand.
    await model.fill("example/not-listed");
    await expect(options).toHaveCount(0);
    await model.fill("example/chat-large");
    await page.getByRole("button", { name: "Save fallback" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Saved." }).last()).toBeVisible();
    await expect(page.getByText("llm.example.org · high effort")).toBeVisible();

    // Move up: the first fallback becomes the primary, without opening the editor.
    await page.getByRole("button", { name: "Move up Chat fallback 1, making it the primary" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Chat: order saved." })).toBeVisible();

    // The studio's cloud backups reorder in place too, and are added, edited and removed here.
    await page.getByRole("button", { name: "Move up Voice notes backup 2" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Voice notes: order saved." })).toBeVisible();
    await page.getByRole("button", { name: "Add backup for video notes" }).click();
    await page.getByLabel("Model", { exact: true }).fill("heygen/heygen-avatar-6-turbo");
    // The key comes from the media provider the voice backups already use.
    await expect(page.getByRole("combobox", { name: "Media provider" })).toHaveText("OpenRouter media");
    await page.getByRole("button", { name: "Add backup", exact: true }).click();
    await expect(page.getByRole("status").filter({ hasText: "Video notes: backup saved." })).toBeVisible();
    await page.getByRole("button", { name: "Move down Video notes backup 1" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Video notes: order saved." })).toBeVisible();
    await page.getByRole("button", { name: "Remove Video notes backup 2" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Remove" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Video notes: backup removed." })).toBeVisible();
    await expect(page.getByText("heygen/heygen-avatar-6-turbo")).toBeVisible();

    // Triage is edited and tested like any role: it picks from OpenRouter's decision models, and its test is a real
    // decisions request.
    await page.getByRole("button", { name: "Edit Triage" }).click();
    await expect(page.getByRole("combobox", { name: "Provider", exact: true })).toHaveText("OpenRouter decisions · OpenRouter");
    const triageModel = page.getByLabel("Model ID").first();
    await triageModel.fill("flash");
    await expect(options).toHaveCount(1);
    await options.first().click();
    await expect(triageModel).toHaveValue("example/decider-flash:free");
    await expect(page.getByText("3 models · fetched")).toBeVisible();
    await page.getByRole("button", { name: "Save triage" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Saved." }).last()).toBeVisible();
    await page.getByRole("button", { name: "Test Triage", exact: true }).click();
    await expect(page.getByText("Decided: yes")).toBeVisible();
    expect(problems).toEqual([]);
  });

  test("chats: send Dom to sleep and wake him, move a slider, reset it, all through confirmed writes", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/chats");
    await page.getByRole("button", { name: "Send Dom to sleep in Your DM" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Dom is asleep in Your DM" })).toBeVisible();
    await page.getByRole("button", { name: "Wake Dom in Your DM" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Dom is up in Your DM" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Send Dom to sleep in Your DM" })).toBeVisible();

    // A slider saves when it is released; the keyboard commits each step.
    await page.getByRole("button", { name: "Personality" }).first().click();
    const humor = page.getByRole("slider", { name: /Humor/ }).first();
    await humor.focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("status").filter({ hasText: /Traits for .*humor/ })).toBeVisible();
    await expect(page.getByText("(custom)")).toBeVisible();
    await page.getByRole("list", { name: "Chats" }).getByRole("button", { name: "Reset to default" }).click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByRole("button", { name: "Keep my changes" }).click();
    await expect(page.getByText("(custom)")).toBeVisible();
    await page.getByRole("list", { name: "Chats" }).getByRole("button", { name: "Reset to default" }).click();
    await dialog.getByRole("button", { name: "Reset to default" }).click();
    await expect(page.getByRole("status").filter({ hasText: "reset to default" })).toBeVisible();
    await expect(page.getByText("(custom)")).toBeHidden();

    // The everywhere preset and a chat's own preset.
    await page.getByRole("button", { name: "Smooth", exact: true }).first().click();
    await expect(page.getByRole("status").filter({ hasText: "Smooth" })).toBeVisible();
    await page.getByRole("button", { name: "Same as everywhere" }).first().click();
    await expect(page.getByRole("status").filter({ hasText: "Preset for" })).toBeVisible();

    // Leaving a group asks first.
    await page.getByRole("button", { name: "Leave Weekend crew" }).click();
    await dialog.getByRole("button", { name: "Stay" }).click();
    await expect(dialog).toBeHidden();
    expect(problems).toEqual([]);
  });

  test("sidebar: folds to a rail and back by button and by [, remembers it, and ignores [ in a text field", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/work/reminders");
    const fold = page.getByRole("button", { name: "Collapse sidebar" });
    await expect(fold).toHaveAttribute("aria-expanded", "true");
    const main = page.locator("#main");
    const wide = (await main.boundingBox())!.x;
    await fold.click();
    const unfold = page.getByRole("button", { name: "Expand sidebar" });
    await expect(unfold).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByRole("heading", { name: "Control surface" })).toHaveClass(/sr-only/); // hidden, still named for screen readers
    const reminders = page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Reminders, in Work" });
    await expect(reminders).toHaveAttribute("aria-current", "page");
    // The page takes the room back: wait for the columns to settle.
    let folded = -1;
    await expect
      .poll(async () => {
        const x = (await main.boundingBox())!.x;
        const settled = x === folded && x < wide;
        folded = x;
        return settled;
      })
      .toBe(true);
    // Keyboard focus on a rail icon shows its label as a tooltip.
    await page.mouse.move(900, 600);
    await reminders.focus();
    await expect(page.locator("[data-slot=tooltip-content]").filter({ hasText: "Reminders" })).toBeVisible();
    // A resting mouse opens the rail for a look, over the page: the page keeps its width and the labels show.
    await reminders.hover();
    await expect(page.locator("aside[data-peek]")).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Main" }).getByText("Control surface")).toBeVisible();
    expect(Math.round((await main.boundingBox())!.x)).toBe(Math.round(folded));
    // The logo spot is the fold button: on a peek it pins the sidebar open.
    await page.getByRole("button", { name: "Expand sidebar" }).click();
    await expect(page.getByRole("button", { name: "Collapse sidebar" })).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator("aside[data-peek]")).toHaveCount(0);
    await expect.poll(async () => Math.round((await main.boundingBox())!.x)).toBe(Math.round(wide));
    await page.mouse.move(900, 600);
    await page.getByRole("button", { name: "Collapse sidebar" }).click();
    await page.mouse.move(900, 600);
    await expect(page.locator("aside[data-peek]")).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole("button", { name: "Expand sidebar" })).toBeVisible();
    // "[" folds and unfolds, except while a text field has the key.
    await page.getByRole("heading", { level: 1 }).click();
    await page.keyboard.press("[");
    await expect(page.getByRole("button", { name: "Collapse sidebar" })).toBeVisible();
    await page.getByRole("searchbox").first().fill("");
    await page.getByRole("searchbox").first().press("[");
    await expect(page.getByRole("button", { name: "Collapse sidebar" })).toBeVisible();
    await expect(page.getByRole("searchbox").first()).toHaveValue("[");
    // "[" on a focused sidebar link keeps focus on that destination in the other layout.
    const nav = page.getByRole("navigation", { name: "Main" });
    await nav.getByRole("link", { name: "Reminders" }).focus();
    await page.keyboard.press("[");
    await expect(nav.getByRole("link", { name: "Reminders, in Work" })).toBeFocused();
    await page.keyboard.press("[");
    await expect(nav.getByRole("link", { name: /^Reminders/ })).toBeFocused();
    expect(problems).toEqual([]);
  });

  test("memory: a file previews as markdown, unsaved edits survive switching tabs, raw HTML stays text", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/memory?file=SOUL.md");
    await page.getByRole("tab", { name: "Edit" }).click();
    const editor = page.getByRole("textbox", { name: "SOUL.md" });
    const original = await editor.inputValue();
    await editor.fill(original + "\n<img src=x onerror=alert(1)> [bad](javascript:alert(1)) [good](https://example.org/a)\n");
    await page.getByRole("tab", { name: "Preview" }).click();
    const preview = page.getByRole("tabpanel");
    await expect(preview.getByRole("heading", { name: "How he talks" })).toBeVisible();
    await expect(preview.getByText("<img src=x onerror=alert(1)>")).toBeVisible();
    await expect(preview.locator("img")).toHaveCount(0);
    await expect(preview.getByRole("link", { name: "bad" })).toHaveCount(0);
    await expect(preview.getByRole("link", { name: "good" })).toHaveAttribute("rel", "noopener noreferrer");
    await expect(page.getByText("Showing your unsaved edits.")).toBeVisible();
    await page.getByRole("tab", { name: "Edit" }).click();
    await expect(page.getByRole("textbox", { name: "SOUL.md" })).toHaveValue(/javascript:alert/);
    await page.getByRole("button", { name: "Discard", exact: true }).click();
    // Dom's own files open as a rendered page.
    await page.getByRole("button", { name: "HABITS.md" }).click();
    await expect(page.getByRole("tab", { name: "Preview" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("tab", { name: "Source" })).toBeVisible();
    expect(problems).toEqual([]);
  });

  test("memory: entity tags read as chips, and an open file stays in view beside a long list", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/memory?file=memory/people/4002.md");
    const preview = page.getByRole("tabpanel");
    await expect(preview.getByText("about: Alex", { exact: true })).toBeVisible();
    await expect(preview.getByText("about: the coast", { exact: true })).toBeVisible();
    await expect(preview).not.toContainText("{entities");
    // The file API's `entities` map names them when it is sent.
    await page.route("**/api/memory/file?*", async (route) => {
      const json = await (await route.fetch()).json();
      await route.fulfill({ json: { ...json, entities: { "person:4002": "Alex Example", "place:the-coast": "The coast" } } });
    });
    await page.reload();
    await expect(page.getByRole("tabpanel").getByText("about: Alex Example", { exact: true })).toBeVisible();
    await expect(page.getByRole("tabpanel").getByText("about: The coast", { exact: true })).toBeVisible();
    await page.unroute("**/api/memory/file?*");
    // Desktop: the list and the open file are each bound to the viewport and scroll on their own.
    const list = page.getByRole("navigation", { name: "Memory files" });
    const bound = await list.evaluate((node) => ({ overflow: getComputedStyle(node).overflowY, height: node.getBoundingClientRect().height }));
    expect(bound.overflow).toBe("auto");
    expect(bound.height).toBeLessThanOrEqual(page.viewportSize()!.height);
    // Phone: the list is a page of its own; a file opened from far down it comes into view at the top.
    await page.setViewportSize(viewports.phone);
    await page.goto("/app/memory");
    const last = page.getByRole("navigation", { name: "Memory files" }).getByRole("button").last();
    await last.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => scrollY)).toBeGreaterThan(0);
    await last.click();
    await expect(page.getByRole("button", { name: "Files", exact: true })).toBeInViewport();
    expect(problems).toEqual([]);
  });

  test("memory: the soul conversation files Dom's edit, which is rejected in place", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/memory");
    await page.getByRole("button", { name: "Talk to Dom about his soul" }).click();
    await expect(page.getByRole("complementary", { name: "SOUL.md" }).getByRole("heading", { name: "How he talks" })).toBeVisible();
    // A failure shows on the message itself, with Retry; the retry then goes through to Dom.
    await page.route("**/api/memory/soul", (route) => route.fulfill({ status: 422, json: { detail: "Couldn't get a reply from Dom. Try again." } }));
    await page.getByRole("textbox", { name: "Message to Dom" }).fill("Would you mention board games?");
    await page.getByRole("button", { name: "Send to Dom" }).click();
    const log = page.getByRole("list", { name: "Conversation" });
    await expect(log.getByRole("alert").filter({ hasText: "Couldn't get a reply from Dom. Try again." })).toBeVisible();
    await page.unroute("**/api/memory/soul");
    await log.getByRole("button", { name: "Retry" }).click();
    await expect(log.getByText("Would you mention board games?")).toBeVisible();
    await expect(log.getByText("Proposed change to SOUL.md")).toBeVisible();
    await expect(log.getByRole("group", { name: "Proposed changes" })).toContainText("Dom likes quiet evenings and the odd board game.");
    await log.getByRole("button", { name: "Reject" }).click();
    await expect(log.getByText("Rejected.")).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Message to Dom" })).toBeEnabled();
    await page.getByRole("button", { name: "New conversation" }).click();
    await expect(log.getByText("Would you mention board games?")).toHaveCount(0);
    expect(problems).toEqual([]);
  });

  test("memory: edit and save a file, a dirty draft asks before leaving, a waiting edit can be approved", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/memory");
    await page.getByRole("tab", { name: "Edit" }).click();
    const editor = page.getByRole("textbox", { name: "MEMORY.md" });
    await expect(editor).toHaveValue(/Travel notes/);
    const original = await editor.inputValue();
    await editor.fill(original + "- Prefers aisle seats.\n");
    const save = page.getByRole("button", { name: "Save changes" });
    await save.click();
    await expect(page.getByRole("status").filter({ hasText: "Saved MEMORY.md." })).toBeVisible();
    await page.reload();
    await page.getByRole("tab", { name: "Edit" }).click();
    await expect(page.getByRole("textbox", { name: "MEMORY.md" })).toHaveValue(/aisle seats/);

    // Unsaved edits ask before another file opens.
    await page.getByRole("textbox", { name: "MEMORY.md" }).fill("scratch");
    await page.getByRole("button", { name: "river", exact: true }).click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByRole("button", { name: "Keep editing" }).click();
    await expect(page.getByRole("textbox", { name: "MEMORY.md" })).toHaveValue("scratch");
    await page.getByRole("button", { name: "river", exact: true }).click();
    await dialog.getByRole("button", { name: "Discard changes" }).click();
    await expect(page.getByRole("tabpanel")).toContainText("River prefers the early train.");

    // Put MEMORY.md back as it was.
    await page.getByRole("button", { name: "MEMORY.md", exact: true }).click();
    await page.getByRole("tab", { name: "Edit" }).click();
    await page.getByRole("textbox", { name: "MEMORY.md" }).fill(original);
    await save.click();
    await expect(page.getByRole("status").filter({ hasText: "Saved MEMORY.md." })).toBeVisible();

    // The private files are listed apart, and Reject on an import waits for its confirm dialog.
    await expect(page.getByRole("heading", { name: "Only in your DM" })).toBeVisible();
    await page.getByRole("button", { name: "Reject all" }).click();
    await dialog.getByRole("button", { name: "Keep them" }).click();
    await expect(dialog).toBeHidden();
    expect(problems).toEqual([]);
  });

  test("work: a handoff answers, destructive actions ask first, a skill retires and comes back", async ({ page }) => {
    const problems = await watch(page);
    const dialog = page.getByRole("alertdialog");

    // The preview's handoff has no live worker behind it, so Dom says it already ended: a refusal, not a change.
    await page.goto("/app/work");
    await expect(page.getByText("Dom needs you to log in: Sign in to continue.")).toBeVisible();
    await expect(page.getByRole("link", { name: "Open live viewer" })).toHaveAttribute("href", "/desktop/vnc/index.html");
    await page.getByRole("button", { name: "I'm done, resume Dom" }).click();
    await expect(page.getByRole("status").filter({ hasText: "The handoff has already ended." })).toBeVisible();
    // One list holds both kinds; a finished task's trash glyph and its details' Delete both ask first.
    const list = page.getByRole("group", { name: "Tasks, newest first" });
    await list.getByRole("button", { name: "Delete desktop task 2" }).click();
    await dialog.getByRole("button", { name: "Keep it" }).click();
    await expect(dialog).toBeHidden();
    await list.getByRole("button", { name: /^Look up opening hours for the library/ }).click();
    const panel = page.getByRole("region", { name: "Look up opening hours for the library." });
    await expect(panel.getByText("Result, as the web gave it (not checked)")).toBeVisible();
    await panel.getByRole("button", { name: "Delete task" }).click();
    await dialog.getByRole("button", { name: "Keep it" }).click();

    // Dom's own work: a planned task cancels from its details, a finished one shows its screenshot.
    await list.getByRole("button", { name: /^Read about slow-travel itineraries/ }).click();
    await page.getByRole("region", { name: "Read about slow-travel itineraries" }).getByRole("button", { name: "Cancel task" }).click();
    await dialog.getByRole("button", { name: "Keep it" }).click();
    await list.getByRole("button", { name: "Delete own work task 2" }).click();
    await dialog.getByRole("button", { name: "Keep it" }).click();
    await list.getByRole("button", { name: /^Check the weather page for Saturday/ }).click();
    const shot = page.getByRole("img", { name: "Screenshot weather.png from task 2" });
    await expect(shot).toBeVisible();
    await expect.poll(() => shot.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);

    await page.goto("/app/work/own-work");
    await page.getByRole("button", { name: "Retire trip-notes" }).click();
    await dialog.getByRole("button", { name: "Retire skill" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Retired skill trip-notes." })).toBeVisible();
    await page.getByRole("button", { name: "Restore trip-notes" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Restored skill trip-notes." })).toBeVisible();
    await expect(page.getByRole("button", { name: "Retire trip-notes" })).toBeVisible();

    await page.goto("/app/work/reminders");
    await page.getByRole("button", { name: "Delete reminder 3" }).click();
    await dialog.getByRole("button", { name: "Keep it" }).click();
    await page.getByRole("button", { name: "Cancel reminder 3" }).click();
    await dialog.getByRole("button", { name: "Keep it" }).click();
    await expect(page.getByText("Pack a charger")).toBeVisible();
    // Bulk: pick two, one confirm cancels both.
    await page.getByRole("checkbox", { name: "Select reminder 1" }).click();
    await page.getByRole("checkbox", { name: "Select reminder 2" }).click();
    await expect(page.getByText("2 selected")).toBeVisible();
    await page.getByRole("button", { name: "Cancel 2 reminders selected" }).click();
    await expect(dialog.getByText("Cancel 2 reminders?")).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel reminders" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Cancelled 2 reminders." })).toBeVisible();
    await expect(page.getByText("None selected")).toBeVisible();

    await page.goto("/app/work/briefs");
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.getByRole("button", { name: "Copy Wake groups gently" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Brief copied." })).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toContain("Wake groups gently");
    expect(problems).toEqual([]);
  });

  test("system: a plugin is reviewed before it is approved, panels load as text, waiting items are decided", async ({ page }) => {
    const problems = await watch(page);
    const dialog = page.getByRole("alertdialog");

    // The approved plugin's panel loads by itself and shows only text.
    await page.goto("/app/system/plugins");
    await expect(page.getByText("Two day trips are planned.")).toBeVisible();
    await expect(page.getByRole("cell", { name: "1 h 20 min" })).toBeVisible();
    await page.getByRole("button", { name: "Refresh On the calendar" }).click();
    await page.getByRole("button", { name: "Retire wall_calendar" }).click();
    await dialog.getByRole("button", { name: "Keep it" }).click();
    await expect(dialog).toBeHidden();

    // The review shows the code and the notices; Approve appears only after "I've read this code".
    await page.getByRole("link", { name: "Review code of trip_planner" }).click();
    await expect(page.getByText("def plan(a, b):")).toBeVisible();
    await expect(page.getByText(/can reach the internet/)).toBeVisible();
    await page.getByText(/Test run passed/).click();
    await expect(page.getByText(/Plugin said: .Planned 2 trips/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Approve" })).toBeHidden();
    await page.getByRole("button", { name: "I've read this code" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Review recorded." })).toBeVisible();
    await expect(page.getByRole("button", { name: "Approve" })).toBeVisible();
    await expect(page.getByRole("button", { name: "I've read this code" })).toBeHidden();
    await page.getByRole("button", { name: "Delete", exact: true }).click();
    await dialog.getByRole("button", { name: "Keep it" }).click();
    await expect(dialog).toBeHidden();

    // Waiting for you: every kind has its card, a decision writes through, Reject all on an import asks first.
    await page.goto("/app/waiting");
    await expect(page.getByText("Book swap")).toBeVisible();
    await expect(page.getByText("Leak score 0/3")).toBeVisible();
    await expect(page.getByRole("link", { name: "Review code" })).toHaveAttribute("href", /\/app\/system\/plugins\/trip_planner\//);
    await page.getByRole("button", { name: "Ignore" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Ignored" })).toBeVisible();
    await page.getByRole("button", { name: "Reject all" }).click();
    await dialog.getByRole("button", { name: "Keep them" }).click();
    await expect(dialog).toBeHidden();
    await page.getByRole("button", { name: "Reject" }).first().click();
    await expect(page.getByRole("status").filter({ hasText: "Rejected" })).toBeVisible();
    await page.getByText(/Settled \(/).click();
    await page.getByRole("button", { name: /Delete settled proposal/ }).first().click();
    await dialog.getByRole("button", { name: "Keep it" }).click();

    // The turn log filters by chat with the app's own picker.
    await page.goto("/app/system/turns");
    await expect(page.getByRole("button", { name: /^Held back: leak check/ })).toBeVisible();
    await page.getByRole("combobox", { name: "Chat" }).fill("Week");
    await page.getByRole("option", { name: "Weekend crew" }).click();
    await expect(page).toHaveURL(/\?chat=-4001$/);
    await expect(page.getByRole("button", { name: /^Held back: leak check/ })).toBeHidden();
    expect(problems).toEqual([]);
  });

  test("w8: turns with repeated steps render, a chat links to its turns, segments and plugins answer", async ({ page }) => {
    const problems = await watch(page);
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const dialog = page.getByRole("alertdialog");

    // A turn that searched twice renders (it threw each_key_duplicate and left the log empty).
    await page.goto("/app/system/turns");
    const turns = page.getByRole("group", { name: "Turns, newest first" });
    await expect(turns.getByRole("button")).not.toHaveCount(0);
    await turns.getByRole("button", { name: /^Replied in 1 bubble.*Your DM/ }).click();
    const twice = page.getByRole("region", { name: "Replied in 1 bubble in Your DM" });
    // Both searches are tools of the turn (and one shows again among the stages).
    await expect(twice.locator("dd").getByText("Search", { exact: true })).toHaveCount(2);
    await expect(twice.getByText("Prompt fingerprint").first()).toBeVisible();

    // A chat's messages link to the turn log filtered to that chat, which reads the chat from the address.
    await page.goto("/app/chats");
    await page.getByRole("button", { name: "Messages in Weekend crew" }).click();
    await expect(page.getByRole("button", { name: "About these messages" })).toBeVisible();
    await page.getByRole("link", { name: "Turn log for this chat" }).click();
    await expect(page).toHaveURL(/\/app\/system\/turns\?chat=-4001$/);
    await expect(page.getByRole("combobox", { name: "Chat" })).toHaveValue("Weekend crew");
    await expect(page.getByRole("button", { name: /^Held back: leak check/ })).toBeHidden();

    // A usage segment shows its breakdown on click and from the keyboard.
    await page.goto("/app/system/usage");
    const segment = page.getByRole("button", { name: /Replies: \$/ }).last();
    await segment.focus();
    await page.keyboard.press("Enter");
    const detail = page.getByRole("region", { name: "Segment breakdown" });
    await expect(detail.getByRole("rowheader", { name: "example/chat-model" })).toBeVisible();
    await expect(detail.getByText(/tokens/)).toBeVisible();
    await page.keyboard.press("Enter");
    await expect(detail).toBeHidden();

    // A rejected plugin says so and goes back to review only through its confirm dialog.
    await page.goto("/app/system/plugins");
    await expect(page.getByText("Rejected", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Review tide_times again" }).click();
    await dialog.getByRole("button", { name: "Leave it off" }).click();
    await expect(page.getByRole("button", { name: "Review tide_times again" })).toBeVisible();
    await page.getByRole("button", { name: "Review tide_times again" }).click();
    await dialog.getByRole("button", { name: "Review again" }).click();
    await expect(page.getByRole("status").filter({ hasText: "tide_times is waiting for your review again." })).toBeVisible();
    await expect(page.getByRole("link", { name: "Review code of tide_times" })).toBeVisible();

    // Editing one model slot says why the others wait.
    await page.goto("/app/system");
    await page.getByRole("button", { name: "Edit Drafting" }).click();
    await expect(page.getByText("Finish editing Drafting first.").first()).toBeVisible();
    await page.getByRole("button", { name: "Cancel" }).click();

    // The import prompt is one tap away, and "Other" covers any assistant.
    await page.goto("/app/memory");
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.getByRole("button", { name: "Import memories" }).click();
    await page.getByRole("button", { name: "Copy prompt" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Prompt copied." })).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toContain("Export everything you remember about me.");
    await page.getByText("Show the prompt").click();
    await expect(page.getByText(/one fact per line/)).toBeVisible();
    await page.getByRole("combobox").click();
    await expect(page.getByRole("option", { name: "Other assistant" })).toBeVisible();
    await page.keyboard.press("Escape");
    expect(errors).toEqual([]);
    expect(problems).toEqual([]);
  });

  test("w8 phone: the menu navigates and closes, the tab pill keeps its place, the soul request opens", async ({ page }) => {
    await page.setViewportSize(viewports.phone);
    const problems = await watch(page);
    await page.goto("/app");
    await expect(page.getByRole("navigation", { name: "Main" })).toBeHidden(); // no bottom bar, no rail
    const menu = page.getByRole("button", { name: /^Menu/ });
    await expect(menu).toHaveAccessibleName(/waiting for you/);
    await menu.click();
    const sheet = page.getByRole("dialog");
    await expect(sheet.getByRole("link", { name: /Waiting for you/ })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
    await expect(menu).toBeFocused();
    await menu.click();
    await sheet.getByRole("link", { name: "Briefs" }).click();
    await expect(sheet).toBeHidden();
    await expect(page).toHaveURL(/\/app\/work\/briefs$/);
    await expect(page.getByRole("heading", { level: 1, name: "Briefs" })).toBeVisible();

    // The pill shows the current tab, even when it sits past the pill's right edge.
    const pill = page.getByRole("navigation", { name: "Work pages" }).getByRole("list");
    const current = pill.getByRole("link", { name: "Briefs" });
    const [pillBox, tabBox] = [await pill.boundingBox(), await current.boundingBox()];
    expect(tabBox!.x + tabBox!.width).toBeLessThanOrEqual(pillBox!.x + pillBox!.width + 1);
    expect(await page.evaluate(() => window.scrollY)).toBe(0);

    // The soul request replaces the file list on a phone and takes focus.
    await page.goto("/app/memory");
    await page.getByRole("button", { name: "Talk to Dom about his soul" }).click();
    await expect(page.getByRole("textbox", { name: "Message to Dom" })).toBeFocused();
    await expect(page.getByRole("navigation", { name: "Memory files" })).toBeHidden();
    expect(problems).toEqual([]);
  });

  test("phone: the search page has its own labelled form, prefilled and usable", async ({ page }) => {
    await page.setViewportSize(viewports.phone);
    const problems = await watch(page);
    await page.goto("/app");
    await page.getByRole("button", { name: /^Menu/ }).click();
    await page.getByRole("dialog").getByRole("link", { name: "Search" }).click();
    await expect(page).toHaveURL(/\/app\/search$/);
    const field = page.getByRole("searchbox", { name: "Search Dom" });
    await expect(field).toBeVisible();
    await field.fill("plan");
    await field.press("Enter");
    await expect(page).toHaveURL(/\/app\/search\?q=plan$/);
    await expect(page.getByText("Results for “plan”.")).toBeVisible();
    await page.reload();
    await expect(field).toHaveValue("plan");
    expect(problems).toEqual([]);
  });

  test("deep links: each notice link opens or selects its item, and a gone one says so", async ({ page }) => {
    const problems = await watch(page);
    await page.clock.setFixedTime(PREVIEW_NOW);
    // A desktop task: its details open beside the list.
    await page.goto("/app/work?task=2");
    const task = page.getByRole("region", { name: "Look up opening hours for the library." });
    await expect(task).toBeVisible();
    // Closing it drops the link from the address, so a reload doesn't reopen it.
    await task.getByRole("button", { name: "Close details" }).click();
    await expect(page).toHaveURL(/\/app\/work$/);
    // One of Dom's own tasks, by its own link.
    await page.goto("/app/work?own=2");
    await expect(page.getByRole("region", { name: "Check the weather page for Saturday" })).toBeVisible();
    // Own work's address forwards a task link to the list; on its own it is the Own work page.
    await page.goto("/app/work/own-work?own=1");
    await expect(page).toHaveURL(/\/app\/work\?own=1$/);
    await expect(page.getByRole("region", { name: "Read about slow-travel itineraries" })).toBeVisible();
    await page.goto("/app/work/own-work");
    await expect(page.getByRole("heading", { level: 1, name: "Own work" })).toBeVisible();
    // Gone: a quiet note, dismissed with its glyph.
    await page.goto("/app/work?task=9999");
    await expect(page.getByRole("status").filter({ hasText: "That task is gone." })).toBeVisible();
    await page.getByRole("button", { name: "Dismiss" }).click();
    await expect(page.getByText("That task is gone.")).toBeHidden();

    // A proposal on Waiting: its card is selected in place.
    const waiting = await (await page.request.get("/api/waiting")).json();
    const card = waiting.cards.find((item: { type: string }) => item.type === "proposal");
    await page.goto(`/app/waiting?proposal=${card.id}`);
    await expect(page.locator(`#waiting-proposal-${card.id}`)).toBeFocused();
    await page.goto("/app/waiting?proposal=99999");
    await expect(page.getByRole("status").filter({ hasText: "That proposal is gone." })).toBeVisible();

    // A reminder, by its ID.
    await page.goto("/app/work/reminders?id=3");
    await expect(page.locator("#reminder-row-3")).toBeFocused();
    await expect(page.locator("#reminder-row-3")).toContainText("Pack a charger");
    await page.goto("/app/work/reminders?id=9999");
    await expect(page.getByRole("status").filter({ hasText: "That reminder is gone." })).toBeVisible();

    // Memory: the page itself.
    await page.goto("/app/memory");
    await expect(page.getByRole("heading", { level: 1, name: "Memory" })).toBeVisible();
    expect(problems).toEqual([]);
  });

  test("deep links: an ID no row can have says gone without asking the API, and the page still loads", async ({ page }) => {
    const problems = await watch(page);
    await page.clock.setFixedTime(PREVIEW_NOW);
    const asked: string[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname.startsWith("/api/")) asked.push(url.pathname + url.search);
    });
    for (const bad of ["0", "99999999999999999999", "12abc"]) {
      await page.goto(`/app/work?task=${bad}`);
      await expect(page.getByRole("status").filter({ hasText: "That task is gone." })).toBeVisible();
      await expect(page.getByText("Look up opening hours for the library.").first()).toBeVisible();
      await page.goto(`/app/work?own=${bad}`);
      await expect(page.getByRole("status").filter({ hasText: "That task is gone." })).toBeVisible();
      await page.goto(`/app/work/own-work?own=${bad}`);
      await expect(page.getByRole("status").filter({ hasText: "That task is gone." })).toBeVisible();
      await page.goto(`/app/work/reminders?id=${bad}`);
      await expect(page.getByRole("status").filter({ hasText: "That reminder is gone." })).toBeVisible();
      await expect(page.locator("#reminder-row-3")).toContainText("Pack a charger");
      await page.goto(`/app/waiting?proposal=${bad}`);
      await expect(page.getByRole("status").filter({ hasText: "That proposal is gone." })).toBeVisible();
      await expect(page.getByRole("list", { name: "Waiting for you" })).toBeVisible();
    }
    expect(asked.filter((query) => /[?&](task|own|id)=/.test(query))).toEqual([]);
    expect(problems).toEqual([]);
  });

  test("deep links: Waiting asks for the linked proposal by ID, so a settled one older than the latest still comes", async ({ page }) => {
    const problems = await watch(page);
    const waiting = await (await page.request.get("/api/waiting")).json();
    const settled = waiting.settled[0];
    // The API answers without it unless the page names it: as if it were older than the latest 50.
    await page.route("**/api/waiting?*", async (route) => {
      const response = await route.fetch();
      const named = new URL(route.request().url()).searchParams.get("id");
      const json = await response.json();
      await route.fulfill({ response, json: named === String(settled.id) ? json : { ...json, settled: [] } });
    });
    await page.goto(`/app/waiting?proposal=${settled.id}`);
    await expect(page.locator(`#settled-${settled.id}`)).toBeFocused();
    await expect(page.getByText("That proposal is gone.")).toBeHidden();
    expect(problems).toEqual([]);
  });

  test("work: the refresh brings Dom's own work up to date too, and says when it can't", async ({ page }) => {
    const problems = await watch(page);
    await page.clock.install({ time: PREVIEW_NOW });
    await page.goto("/app/work");
    await expect(page.getByText("Check the weather page for Saturday").first()).toBeVisible();
    let mode: "new" | "fail" = "new";
    await page.route("**/api/work/own*", async (route) => {
      if (mode === "fail") return route.fulfill({ status: 500, json: { detail: "Dom is busy" } });
      const response = await route.fetch();
      const json = await response.json();
      const planned = { ...json.tasks[0], id: 77, goal: "Find a quiet café near the station", status: "planned" };
      await route.fulfill({ response, json: { ...json, tasks: [planned, ...json.tasks] } });
    });
    await page.clock.runFor(4500);
    await expect(page.getByText("Find a quiet café near the station").first()).toBeVisible();
    mode = "fail";
    await page.clock.runFor(4500);
    await expect(page.getByRole("alert").filter({ hasText: "Couldn't refresh Dom's own work" })).toBeVisible();
    await expect(page.getByText("Find a quiet café near the station").first()).toBeVisible();
    expect(problems).toEqual([]);
  });

  test("chats: a chat Dom can't reach is checked, hidden and shown again, and forgetting asks once more for its history", async ({ page }) => {
    const problems = await watch(page);
    const dialog = page.getByRole("alertdialog");
    await page.goto("/app/chats");
    const gone = page.getByRole("list", { name: "Chats Dom can't reach" });
    await expect(gone.getByText("Old book club")).toBeVisible();
    // Its history stays readable.
    await gone.getByRole("button", { name: "Messages in Old book club" }).click();
    await expect(gone.getByText("See you all at the next meeting.")).toBeVisible();

    await page.getByRole("button", { name: "Check now" }).click();
    await expect(page.getByRole("status").filter({ hasText: /Checked \d+ chats?/ })).toBeVisible();

    // Hide moves it under Hidden; Show again brings it back.
    await gone.getByRole("button", { name: "Hide Old book club" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Hid chat." })).toBeVisible();
    await page.getByText("Hidden (2)").click();
    await page.getByRole("button", { name: "Show Old book club again" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Restored chat." })).toBeVisible();
    await expect(gone.getByText("Old book club")).toBeVisible();

    // Forget asks; Keep it changes nothing.
    await gone.getByRole("button", { name: "Forget Old book club" }).click();
    await expect(dialog.getByText("Forget Old book club?")).toBeVisible();
    await dialog.getByRole("button", { name: "Keep it" }).click();
    await expect(dialog).toBeHidden();
    // "Delete history too" is a second, explicit confirm; the request says both. (Answered here, so the preview keeps it.)
    let sent: unknown = null;
    await page.route("**/api/chats/forget", async (route) => {
      sent = route.request().postDataJSON();
      await route.fulfill({ json: { message: "Forgot chat and deleted its history.", ...(await (await page.request.get("/api/chats")).json()) } });
    });
    await gone.getByRole("button", { name: "Forget Old book club" }).click();
    await dialog.getByRole("checkbox", { name: "Delete history too" }).click();
    await dialog.getByRole("button", { name: "Forget chat" }).click();
    await expect(dialog.getByText("Delete every message from Old book club?")).toBeVisible();
    await dialog.getByRole("button", { name: "Keep the history" }).click();
    expect(sent).toBeNull();
    await gone.getByRole("button", { name: "Forget Old book club" }).click();
    await dialog.getByRole("checkbox", { name: "Delete history too" }).click();
    await dialog.getByRole("button", { name: "Forget chat" }).click();
    await dialog.getByRole("button", { name: "Delete history and forget" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Forgot chat and deleted its history." })).toBeVisible();
    expect(sent).toEqual({ chat_id: -4005, delete_history: true, confirm: true });
    expect(problems).toEqual([]);
  });

  test("chats: Fresh start asks first, then starts the chat fresh, and Undo puts the earlier messages back", async ({ page }) => {
    const problems = await watch(page);
    const dialog = page.getByRole("alertdialog");
    const chats = await (await page.request.get("/api/chats")).json();
    const chat = chats.chats[0];
    // Answered here, so the preview's chats keep their window for the other screens.
    const sent: unknown[] = [];
    await page.route("**/api/chats/fresh", async (route) => {
      const body = route.request().postDataJSON();
      sent.push(body);
      const message = body.undo ? `Put the earlier messages back in ${chat.title}.` : `Started ${chat.title} fresh. Nothing was deleted.`;
      await route.fulfill({ json: { message, ...chats } });
    });
    await page.goto("/app/chats");
    await expect(page.getByRole("button", { name: `Undo the fresh start in ${chat.title}` })).toHaveCount(0);
    // Not now changes nothing.
    await page.getByRole("button", { name: `Fresh start in ${chat.title}` }).click();
    await expect(dialog.getByText(`Start ${chat.title} fresh?`)).toBeVisible();
    await expect(dialog.getByText("Earlier messages leave Dom's prompt; nothing is deleted.")).toBeVisible();
    await dialog.getByRole("button", { name: "Not now" }).click();
    await expect(dialog).toBeHidden();
    expect(sent).toEqual([]);
    // Start fresh sends the chat; the toast says so, and Undo appears for that chat.
    await page.getByRole("button", { name: `Fresh start in ${chat.title}` }).click();
    await dialog.getByRole("button", { name: "Start fresh" }).click();
    await expect(page.getByRole("status").filter({ hasText: `Started ${chat.title} fresh.` })).toBeVisible();
    expect(sent).toEqual([{ chat_id: chat.id, undo: false }]);
    const undo = page.getByRole("button", { name: `Undo the fresh start in ${chat.title}` });
    await undo.click();
    await expect(page.getByRole("status").filter({ hasText: `Put the earlier messages back in ${chat.title}.` })).toBeVisible();
    expect(sent).toEqual([{ chat_id: chat.id, undo: false }, { chat_id: chat.id, undo: true }]);
    await expect(undo).toHaveCount(0);
    expect(problems).toEqual([]);
  });

  test("memory: preview first, Edit on demand, tags as chips, core memories unpin through the save", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/memory?file=memory/people/4002.md");
    // Preview by default: rendered, no editor until Edit.
    await expect(page.getByRole("tab", { name: "Preview" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("textbox", { name: "Alex" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Save changes" })).toHaveCount(0);
    const preview = page.getByRole("tabpanel");
    await expect(preview.getByText("from: Weekend crew").first()).toBeVisible();
    await expect(preview.getByText("core, pinned by you")).toBeVisible();
    await expect(preview).not.toContainText("{chat");
    await page.getByRole("tab", { name: "Edit" }).click();
    await expect(page.getByRole("textbox", { name: "Alex" })).toHaveValue(/\{core owner 2026-11-21\}/);
    await expect(page.getByRole("button", { name: "Save changes" })).toBeDisabled();
    await page.getByRole("tab", { name: "Preview" }).click();
    await expect(page.getByRole("textbox", { name: "Alex" })).toBeHidden();

    // The journal keeps its lines apart; the archive is read-only.
    await page.getByRole("button", { name: "Journal", exact: true }).click();
    await expect(page.getByRole("tabpanel").locator("br")).not.toHaveCount(0);
    await page.getByText("Archive").click();
    await page.getByRole("button", { name: "Journal, September 2026" }).click();
    await expect(page.getByText("Archive, read-only")).toBeVisible();
    await expect(page.getByRole("tab", { name: "Source" })).toBeVisible();
    await page.getByRole("button", { name: "Alex (former)" }).click();
    await expect(page.getByRole("tabpanel")).toContainText("Works at the bakery");

    // Core memories: who pinned each, and a delete that offers to unpin instead. The save is answered here.
    let saved: { file: string; content: string } | null = null;
    await page.route("**/api/memory/save", async (route) => {
      saved = route.request().postDataJSON();
      await route.fulfill({ json: { message: "Saved Alex.", content: saved!.content, version: "v" } });
    });
    await page.getByRole("button", { name: "Core memories" }).click();
    const core = page.getByRole("list", { name: "Core memories" });
    await expect(core.getByText("Pinned by Dom")).toBeVisible();
    await expect(core.getByText("Why: she talks about it all the time")).toBeVisible();
    await core.getByRole("button", { name: "Delete core memory: Allergic to peanuts" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Unpin, keep the line" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Unpinned." })).toBeVisible();
    expect(saved!.file).toBe("memory/people/4002.md");
    expect(saved!.content).toContain("- Allergic to peanuts\n");
    expect(saved!.content).toContain("{core dom 2026-11-20: she talks about it all the time}");

    // Import sits beside Export, in a sheet.
    await page.getByRole("button", { name: "Import memories" }).click();
    await expect(page.getByRole("dialog", { name: "Import memories" })).toBeVisible();
    await page.getByRole("button", { name: "Close import" }).click();
    await expect(page.getByRole("dialog", { name: "Import memories" })).toBeHidden();
    expect(problems).toEqual([]);
  });

  test("restart: the banner's button confirms, says why it was refused, then waits for a new Dom and reloads", async ({ page }) => {
    const problems = await watch(page);
    await page.route("**/api/models", async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...await response.json(), restart_required: true } });
    });
    let refuse = true;
    const restarts: unknown[] = [];
    await page.route("**/api/system/restart", async (route) => {
      restarts.push(route.request().postDataJSON());
      if (refuse) await route.fulfill({ status: 409, json: { detail: "A desktop task is running. Wait for it to finish or cancel it first." } });
      else await route.fulfill({ json: { ok: true, message: "Restarting…", boot: "old-boot" } });
    });
    let polls = 0;
    await page.route("**/api/health", async (route) => {
      polls += 1;
      // Down once, then the same process, then a new one: only the new one reloads the page.
      if (polls === 1) await route.abort();
      else await route.fulfill({ json: { ok: true, boot: polls < 3 ? "old-boot" : "new-boot", version: "v0.0.0" } });
    });
    await page.goto("/app/system");
    const banner = page.getByRole("region", { name: "Restart Dom to apply" });
    await expect(banner).toBeVisible();
    await expect(banner).toHaveScreenshot("restart-banner-desktop-dark.png");

    // Not now leaves Dom alone; the refusal shows its reason and keeps the button.
    await banner.getByRole("button", { name: "Restart Dom" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Not now" }).click();
    expect(restarts).toEqual([]);
    await banner.getByRole("button", { name: "Restart Dom" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Restart", exact: true }).click();
    await expect(banner.getByRole("alert")).toHaveText("Not restarted: A desktop task is running. Wait for it to finish or cancel it first.");
    await expect(banner.getByRole("button", { name: "Restart Dom" })).toBeEnabled();
    expect(polls).toBe(0);

    refuse = false;
    await page.evaluate(() => ((window as unknown as { marker: number }).marker = 1));
    await banner.getByRole("button", { name: "Restart Dom" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Restart", exact: true }).click();
    await expect(page.getByRole("status").filter({ hasText: "Restarting…" })).toBeVisible();
    // The reload drops the marker the old page set.
    await expect.poll(() => page.evaluate(() => (window as unknown as { marker?: number }).marker), { timeout: 15_000 }).toBeUndefined();
    expect(polls).toBeGreaterThanOrEqual(3);
    expect(restarts).toEqual([{}, {}]);
    expect(problems).toEqual([]);
  });

  test("spend: the cap editor checks each amount, the monthly cap against the daily, and saves every cap", async ({ page }) => {
    const problems = await watch(page);
    const saved: Record<string, unknown>[] = [];
    await page.route("**/api/spend/caps", async (route) => {
      saved.push(route.request().postDataJSON());
      await route.fulfill({ json: await (await page.request.get("/api/spend")).json() });
    });
    await page.goto("/app/system/spend");
    const caps = page.getByRole("region", { name: "Caps" });
    const save = caps.getByRole("button", { name: "Save caps" });
    await expect(caps.getByRole("textbox", { name: "Daily cap", exact: true })).toHaveValue("1");
    await expect(caps.getByRole("textbox", { name: "Monthly cap", exact: true })).toHaveValue("30");
    await expect(save).toBeDisabled(); // nothing changed yet

    await caps.getByRole("textbox", { name: "Monthly cap", exact: true }).fill("0.5");
    await expect(caps.getByRole("alert")).toHaveText("The monthly cap must be at least the daily cap.");
    await expect(caps.getByRole("textbox", { name: "Monthly cap", exact: true })).toHaveAttribute("aria-invalid", "true");
    await expect(save).toBeDisabled();
    await caps.getByRole("textbox", { name: "Daily cap", exact: true }).fill("two dollars");
    await expect(caps.getByRole("alert").first()).toHaveText("Enter an amount from 0 to 100,000, or leave it empty.");
    await caps.getByRole("textbox", { name: "Web search", exact: true }).fill("-1");
    await expect(caps.getByRole("alert")).toHaveCount(2);

    await caps.getByRole("textbox", { name: "Daily cap", exact: true }).fill("2");
    await caps.getByRole("textbox", { name: "Monthly cap", exact: true }).fill(""); // empty: no monthly cap
    await caps.getByRole("textbox", { name: "Web search", exact: true }).fill("1.5");
    await caps.getByRole("switch", { name: "Caps apply to your DM too" }).click();
    await expect(caps.getByRole("alert")).toHaveCount(0);
    await save.click();
    await expect.poll(() => saved.length).toBe(1);
    expect(saved[0]).toEqual({
      revision: expect.any(String), daily_spend_cap_usd: 2, monthly_spend_cap_usd: 0, cap_owner_dm: true,
      area_caps: { chat: null, media: 5, search: 1.5, desktop: null, memory: null },
    });
    // The table carries every figure the chart shows.
    await page.getByText("Show as a table").click();
    await expect(page.getByRole("table", { name: "Daily spend by area, last 30 days" }).getByRole("row")).toHaveCount(31);
    expect(problems).toEqual([]);
  });

  test("services: a secret is never shown or echoed, only set or not set, and the box empties after a save", async ({ page }) => {
    const problems = await watch(page);
    const view = await (await page.request.get("/api/services")).json();
    const klipy = view.services.find((service: { id: string }) => service.id === "klipy");
    // The API itself never carries a secret's value.
    for (const field of klipy.fields) if (field.kind === "secret") expect(field.value).toBeNull();
    const bodies: Record<string, unknown>[] = [];
    await page.route("**/api/services/save", async (route) => {
      bodies.push(route.request().postDataJSON());
      const after = structuredClone(view);
      after.services.find((service: { id: string }) => service.id === "klipy").fields[0].is_set = true;
      await route.fulfill({ json: after });
    });
    await page.goto("/app/system");
    await page.getByRole("tab", { name: "Services" }).click();
    const card = page.getByRole("region", { name: "GIFs (KLIPY)" });
    await expect(card.getByText("Not set", { exact: true })).toBeVisible();
    const box = card.getByRole("textbox", { name: "KLIPY API key", exact: true });
    await expect(box).toHaveAttribute("type", "password");
    await expect(box).toHaveValue("");
    await box.fill("example-made-up-key");
    await card.getByRole("button", { name: "Save GIFs (KLIPY)" }).click();
    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0]).toEqual({ revision: view.revision, id: "klipy", fields: { [klipy.fields[0].name]: "example-made-up-key" } });
    await expect(card.getByText("Set", { exact: true })).toBeVisible();
    await expect(box).toHaveValue("");
    expect(await page.content()).not.toContain("example-made-up-key");
    expect(problems).toEqual([]);
  });

  test("home: For you's buttons hand a noticing to Dom or let it go, and it leaves the list", async ({ page }) => {
    const problems = await watch(page);
    const decided: unknown[] = [];
    await page.route("**/api/noticings/decide", async (route) => {
      const body = route.request().postDataJSON();
      decided.push(body);
      await route.fulfill({ json: { ok: true, message: body.decision === "accept" ? "Dom's on it." : "Let go.", noticing: null, noticings: [], pending_count: 0, kinds: [], statuses: [] } });
    });
    await page.goto("/app");
    const forYou = page.getByRole("region", { name: "For you" }).or(page.locator('[aria-labelledby="for-you-title"]'));
    const ids = (await (await page.request.get("/api/noticings")).json()).noticings.map((item: { id: number; title: string }) => item);
    const train = ids.find((item: { title: string }) => item.title.startsWith("Saturday"));
    const alex = ids.find((item: { title: string }) => item.title.startsWith("Ask Alex"));
    await forYou.getByRole("button", { name: /^Let's do it\s*: Saturday's train/ }).click();
    await expect(forYou.getByText("Saturday's train needs booking")).toHaveCount(0);
    await expect(page.getByText("Dom's on it.")).toBeVisible();
    await forYou.getByRole("button", { name: /^Not now\s*: Ask Alex/ }).click();
    await expect(forYou.getByText("Ask Alex how the night shift went")).toHaveCount(0);
    expect(decided).toEqual([{ id: train.id, decision: "accept" }, { id: alex.id, decision: "dismiss" }]);
    await expect(forYou.getByText("Nothing new.", { exact: false })).toBeVisible();
    expect(problems).toEqual([]);
  });

  test("home: goals are added, edited and paused, and Done asks first", async ({ page }) => {
    const problems = await watch(page);
    const writes: [string, unknown][] = [];
    const listing = await (await page.request.get("/api/goals")).json();
    await page.route("**/api/goals/*", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      writes.push([new URL(route.request().url()).pathname, route.request().postDataJSON()]);
      await route.fulfill({ json: { ok: true, message: "Saved.", ...listing } });
    });
    await page.goto("/app");
    const goals = page.locator('[aria-labelledby="goals-title"]');
    const walk = listing.goals.find((goal: { text: string }) => goal.text.startsWith("Walk"));

    await goals.getByRole("button", { name: "Add a goal" }).click();
    await goals.getByRole("textbox", { name: "Goal", exact: true }).fill("Read one book a month");
    await goals.getByRole("button", { name: "Add goal" }).click();
    await expect.poll(() => writes.length).toBe(1);

    await goals.getByRole("button", { name: `Edit goal: ${walk.text}` }).click();
    await expect(goals.getByRole("textbox", { name: "Goal", exact: true })).toHaveValue(walk.text);
    await goals.getByRole("textbox", { name: "Goal", exact: true }).fill("Walk 9,000 steps most days");
    await goals.getByRole("button", { name: "Save goal" }).click();
    await expect.poll(() => writes.length).toBe(2);

    await goals.getByRole("button", { name: `Pause goal: ${walk.text}` }).click();
    await expect.poll(() => writes.length).toBe(3);
    await goals.getByRole("button", { name: `Mark done: ${walk.text}` }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog.getByText("Mark this goal done?")).toBeVisible();
    await dialog.getByRole("button", { name: "Keep it" }).click();
    expect(writes).toHaveLength(3);
    await goals.getByRole("button", { name: `Mark done: ${walk.text}` }).click();
    await dialog.getByRole("button", { name: "Mark done" }).click();
    await expect.poll(() => writes.length).toBe(4);
    expect(writes).toEqual([
      ["/api/goals/create", { text: "Read one book a month", category: "", target_date: null, check_in_rhythm: "" }],
      ["/api/goals/update", { id: walk.id, text: "Walk 9,000 steps most days", category: "health", check_in_rhythm: "every_few_days", clear_target_date: true }],
      ["/api/goals/status", { id: walk.id, status: "paused" }],
      ["/api/goals/status", { id: walk.id, status: "done" }],
    ]);
    expect(problems).toEqual([]);
  });

  test("search: the mode sits in the field, words stay the default, a new mode searches again and is remembered", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/search?q=train");
    const modes = page.getByRole("main").getByRole("group", { name: "Search mode" });
    const words = modes.getByRole("button", { name: "Search by words" });
    const meaning = modes.getByRole("button", { name: "Search by meaning" });
    await expect(words).toHaveAttribute("aria-pressed", "true");
    await expect(meaning).toHaveAttribute("aria-pressed", "false");
    const asked = page.waitForRequest((request) => request.url().includes("/api/search?") && request.url().includes("mode=meaning"));
    await meaning.click();
    await asked;
    await expect(page).toHaveURL(/\/app\/search\?q=train&mode=meaning$/);
    await expect(meaning).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByRole("banner").getByRole("button", { name: "Search by meaning" })).toHaveAttribute("aria-pressed", "true");
    // The choice is remembered: the top box starts in meaning on another page, until words is picked again.
    await page.goto("/app/work/reminders");
    await expect(page.getByRole("banner").getByRole("button", { name: "Search by meaning" })).toHaveAttribute("aria-pressed", "true");
    await page.goto("/app/search?q=train&mode=meaning");
    const keyword = page.waitForRequest((request) => /\/api\/search\?q=train$/.test(request.url()));
    await page.getByRole("main").getByRole("button", { name: "Search by words" }).click();
    await keyword;
    await expect(page).toHaveURL(/\/app\/search\?q=train$/);
    expect(problems).toEqual([]);
  });

  test("search: hits sort by score, show their strength quietly, and work without scores", async ({ page }) => {
    const problems = await watch(page);
    const hit = (title: string, extra: Record<string, unknown> = {}) => ({ title, text: `${title} text`, href: "/app/memory", at: "", full: true, ...extra });
    await page.route("**/api/search?*", (route) =>
      route.fulfill({
        json: {
          query: "train",
          mode: "meaning",
          groups: [
            { kind: "memory", label: "Memory", hits: [hit("Weak one", { score: 0.38, match: "weak" }), hit("Strong one", { score: 0.81, match: "strong" })] },
            { kind: "messages", label: "Messages", hits: [hit("Best overall", { score: 0.9, match: "strong" }), hit("No score")] },
          ],
        },
      }));
    await page.goto("/app/search?q=train&mode=meaning");
    await expect(page.locator("section[aria-labelledby^='search-'] [data-slot='card-title']")).toHaveText(["Messages", "Memory"]);
    const memory = page.getByRole("region", { name: "Memory" }).getByRole("link");
    await expect(memory.first()).toContainText("Strong one");
    await expect(memory.first()).toContainText("strong match");
    await expect(memory.nth(1)).toContainText("weak match");
    await expect(page.getByRole("region", { name: "Messages" }).getByRole("link").nth(1)).not.toContainText("match");
    expect(problems).toEqual([]);
  });

  test("settings: a staged embedding model reindexes only on Confirm, shows progress, and Cancel stops it", async ({ page }) => {
    const problems = await watch(page);
    let state: "idle" | "running" = "idle";
    const actions: string[] = [];
    await page.route("**/api/memory/index/estimate", (route) =>
      route.fulfill({ json: { items: 240, rough_seconds: 132, model: "example/embed-small", staged: state === "idle", rebuild: false } }));
    await page.route("**/api/memory/index/progress", (route) =>
      route.fulfill({ json: { done: state === "running" ? 60 : 0, left: state === "running" ? 180 : 240, model: state === "running" ? "example/embed-small" : "", state, last_error: "", stale: false } }));
    await page.route(/\/api\/memory\/index\/(confirm|cancel)$/, async (route) => {
      const action = new URL(route.request().url()).pathname.split("/").pop()!;
      actions.push(action);
      state = action === "confirm" ? "running" : "idle";
      await route.fulfill({ json: { ok: true } });
    });
    await page.goto("/app/system");
    const index = page.getByRole("region", { name: "Meaning index" });
    await expect(index.getByText("example/embed-small")).toBeVisible();
    await expect(index.getByText("240 items, about 2 minutes.", { exact: false })).toBeVisible();
    expect(actions).toEqual([]); // nothing starts until Confirm
    await index.getByRole("button", { name: "Confirm reindex" }).click();
    await expect(index.getByText("Running")).toBeVisible();
    await expect(index.getByRole("progressbar", { name: "Items indexed" })).toBeVisible();
    await index.getByRole("button", { name: "Cancel reindex" }).click();
    await expect(index.getByText("Idle")).toBeVisible();
    expect(actions).toEqual(["confirm", "cancel"]);
    expect(problems).toEqual([]);
  });

  test("settings: What Dom may do narrows by search and keeps its sections", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/system");
    await page.getByRole("tab", { name: "What Dom may do" }).click();
    for (const name of ["Messaging", "Media", "Desktop", "Memory", "Safety"]) {
      await expect(page.getByRole("heading", { level: 2, name, exact: true })).toBeVisible();
    }
    await page.getByLabel("Find a setting").fill("leak");
    await expect(page.getByRole("heading", { level: 2, name: "Safety" })).toBeVisible();
    await expect(page.getByRole("heading", { level: 2, name: "Messaging" })).toHaveCount(0);
    await expect(page.getByRole("radio", { name: "Standard" })).toBeAttached();
    await page.getByLabel("Find a setting").fill("nothing like this");
    await expect(page.getByText("No setting matches")).toBeVisible();
    expect(problems).toEqual([]);
  });

  test("keyboard focus is visible and the skip link leads to the page", async ({ page }) => {
    await page.goto("/app");
    await page.keyboard.press("Tab");
    const skip = page.getByRole("link", { name: "Skip to content" });
    await expect(skip).toBeFocused();
    await expect(skip).toBeVisible();
    await page.keyboard.press("Tab");
    const outline = await page.evaluate(() => getComputedStyle(document.activeElement!).outlineStyle);
    expect(outline).toBe("solid");
  });

  test("links inside /app change the page without a reload, and the theme switch persists", async ({ page }) => {
    await page.goto("/app");
    await page.getByRole("list", { name: "Control surface" }).getByRole("link", { name: "System" }).click();
    await expect(page).toHaveURL(/\/app\/system$/);
    await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
    await page.getByRole("navigation", { name: "System pages" }).getByRole("link", { name: "Usage" }).click();
    await expect(page).toHaveURL(/\/app\/system\/usage$/);
    await expect(page.getByRole("heading", { level: 1, name: "Usage" })).toBeVisible();
    await page.getByRole("button", { name: "Switch to the light theme" }).click();
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  });

  test("tooltips don't animate under reduced motion", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/app/gallery");
    await page.getByRole("button", { name: "Edit reminder" }).focus();
    const tip = page.locator("[data-slot=tooltip-content]").filter({ hasText: "Edit reminder" });
    await expect(tip).toBeVisible();
    expect(await tip.evaluate((node) => getComputedStyle(node).animationName)).toBe("none");
  });

  test("the orbit is still under reduced motion", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/app/gallery");
    const names = await page.locator("svg.orbit .gimbal").evaluateAll((nodes) =>
      nodes.map((node) => getComputedStyle(node).animationName),
    );
    expect(names.length).toBeGreaterThan(0);
    expect(new Set(names)).toEqual(new Set(["none"]));
  });
});
