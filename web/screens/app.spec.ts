import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { PREVIEW_PORT as PORT } from "./preview";

const viewports = { phone: { width: 390, height: 844 }, desktop: { width: 1280, height: 900 } } as const;
const pages: { name: string; path: string; heading: string; tab?: string; click?: string; link?: string }[] = [
  // The shell on a page that shows where the utility pages live (Work's tabs, the sidebar's Workspace group).
  { name: "shell", path: "/app/work/reminders", heading: "Reminders" },
  { name: "home", path: "/app", heading: "Good evening." },
  { name: "gallery", path: "/app/gallery", heading: "Component library" },
  { name: "settings-models", path: "/app/system", heading: "Settings" },
  { name: "settings-providers", path: "/app/system", heading: "Settings", tab: "Providers" },
  { name: "settings-permissions", path: "/app/system", heading: "Settings", tab: "What Dom may do" },
  { name: "chats", path: "/app/chats", heading: "Chats" },
  { name: "chats-personality", path: "/app/chats", heading: "Chats", click: "Personality" },
  { name: "memory", path: "/app/memory", heading: "Memory" },
  // On a phone the list is its own screen and a file opens full width; on a desktop the list stays beside it.
  { name: "memory-file", path: "/app/memory", heading: "Memory", click: "MEMORY.md" },
  // Work: the desk with a handoff waiting for the owner, Dom's own tasks and skills, reminders and a brief.
  { name: "work-desk", path: "/app/work", heading: "Desk" },
  { name: "work-own", path: "/app/work/own-work", heading: "Own work" },
  { name: "work-reminders", path: "/app/work/reminders", heading: "Reminders" },
  { name: "work-briefs", path: "/app/work/briefs", heading: "Briefs" },
  // System and Waiting: plugins with their panel, a plugin's code review, spend, the turn log and what waits for the owner.
  { name: "system-plugins", path: "/app/system/plugins", heading: "Plugins" },
  { name: "system-plugin-review", path: "/app/system/plugins", heading: "Plugins", link: "Review code of trip_planner" },
  { name: "system-usage", path: "/app/system/usage", heading: "Usage" },
  { name: "system-turns", path: "/app/system/turns", heading: "Turn log" },
  { name: "waiting", path: "/app/waiting", heading: "Waiting for you" },
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

for (const scheme of ["dark", "light"] as const) {
  for (const [device, viewport] of Object.entries(viewports)) {
    test.describe(`${device} ${scheme}`, () => {
      test.use({ viewport, colorScheme: scheme });

      for (const { name, path, heading, tab, click, link } of pages) {
        test(name, async ({ page }) => {
          const problems = await watch(page);
          const response = await page.goto(path);
          expect(response?.headers()["content-security-policy"]).toContain("default-src 'self'");
          await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
          if (link) {
            await page.getByRole("link", { name: link }).click();
            await expect(page.getByRole("button", { name: "I've read this code" })).toBeVisible();
          }
          if (tab) await page.getByRole("tab", { name: tab }).click();
          if (click) await page.getByRole("button", { name: click, exact: true }).first().click();
          await page.waitForLoadState("networkidle");
          await page.evaluate(() => document.fonts.ready);
          // Grow the viewport to the whole page, so the phone's bottom bar and the desktop sidebar sit where a
          // person sees them instead of where a full-page capture would stitch them.
          const height = await page.evaluate(() => document.documentElement.scrollHeight);
          await page.setViewportSize({ width: viewport.width, height: Math.max(viewport.height, height) });
          await expect(page).toHaveScreenshot(`${name}-${device}-${scheme}.png`);
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

  test("select, dialog and tooltip open without CSP violations", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/gallery");
    await page.getByRole("combobox").click();
    await page.getByRole("option", { name: "OpenRouter" }).click();
    await expect(page.getByRole("combobox")).toHaveText(/OpenRouter/);
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

    await page.getByRole("tab", { name: "Models" }).click();
    await page.getByRole("button", { name: "Test Chat", exact: true }).click();
    await expect(page.getByText(/OK · \d+\.\d s/).first()).toBeVisible();
    // Every saved slot has its own Test, fallbacks included.
    await page.getByRole("button", { name: "Test Drafting fallback 1" }).click();
    await expect(page.getByText(/OK · \d+\.\d s/)).toHaveCount(2);

    // The editor opens on the saved chain and cancels without writing.
    await page.getByRole("button", { name: "Edit Drafting" }).click();
    await expect(page.getByLabel("Base URL").first()).toHaveValue("https://drafts.example.net/v1");
    await page.getByRole("button", { name: "Cancel" }).click();
    await expect(page.getByRole("button", { name: "Edit Drafting" })).toBeEnabled();
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

  test("memory: edit and save a file, a dirty draft asks before leaving, a waiting edit can be approved", async ({ page }) => {
    const problems = await watch(page);
    await page.goto("/app/memory");
    const editor = page.getByRole("textbox", { name: "MEMORY.md" });
    await expect(editor).toHaveValue(/Travel notes/);
    const original = await editor.inputValue();
    await editor.fill(original + "- Prefers aisle seats.\n");
    const save = page.getByRole("button", { name: "Save changes" });
    await save.click();
    await expect(page.getByRole("status").filter({ hasText: "Saved MEMORY.md." })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("textbox", { name: "MEMORY.md" })).toHaveValue(/aisle seats/);

    // Unsaved edits ask before another file opens.
    await page.getByRole("textbox", { name: "MEMORY.md" }).fill("scratch");
    await page.getByRole("button", { name: "river.md" }).click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByRole("button", { name: "Keep editing" }).click();
    await expect(page.getByRole("textbox", { name: "MEMORY.md" })).toHaveValue("scratch");
    await page.getByRole("button", { name: "river.md" }).click();
    await dialog.getByRole("button", { name: "Discard changes" }).click();
    await expect(page.getByRole("textbox", { name: "river.md" })).toHaveValue(/early train/);

    // Put MEMORY.md back as it was.
    await page.getByRole("button", { name: "MEMORY.md", exact: true }).click();
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
    await page.getByRole("button", { name: "Delete desktop task 2" }).click();
    await dialog.getByRole("button", { name: "Keep it" }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText("Look up opening hours for the library.")).toBeVisible();

    await page.goto("/app/work/own-work");
    await page.getByRole("button", { name: "Cancel #1" }).click();
    await dialog.getByRole("button", { name: "Keep it" }).click();
    await page.getByRole("button", { name: "Delete #2" }).click();
    await dialog.getByRole("button", { name: "Keep it" }).click();
    await expect(page.getByRole("img", { name: "Screenshot weather.png from task 2" })).toBeVisible();
    expect(await page.getByRole("img", { name: "Screenshot weather.png from task 2" }).evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
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

    // The turn log filters by chat.
    await page.goto("/app/system/turns");
    await page.getByLabel("Chat").selectOption({ label: "Weekend crew" });
    await expect(page.getByText("Held back: leak check")).toBeHidden();
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
