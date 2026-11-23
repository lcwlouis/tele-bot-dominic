import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { PREVIEW_PORT as PORT } from "./preview";

const viewports = { phone: { width: 390, height: 844 }, desktop: { width: 1280, height: 900 } } as const;
const pages: { name: string; path: string; heading: string; tab?: string; click?: string }[] = [
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

      for (const { name, path, heading, tab, click } of pages) {
        test(name, async ({ page }) => {
          const problems = await watch(page);
          const response = await page.goto(path);
          expect(response?.headers()["content-security-policy"]).toContain("default-src 'self'");
          await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
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
    await expect(page.getByRole("link", { name: "Open Usage in the current dashboard" })).toHaveAttribute("href", "/usage");
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
