import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { expect, test, type Page } from '@playwright/test';

const AXE = readFileSync(createRequire(import.meta.url).resolve('axe-core/axe.min.js'), 'utf8');

const PAGES = [
  { name: 'home', hash: '#/' },
  { name: 'chats', hash: '#/chats' },
  { name: 'settings', hash: '#/settings' },
] as const;

const WIDTHS = [
  { name: 'phone', width: 390, height: 844 },
  { name: 'desktop', width: 1280, height: 800 },
] as const;

const THEMES = ['light', 'dark'] as const;

/** Open a page and fail on anything the CSP blocks or the console reports as an error. */
async function open(page: Page, hash: string): Promise<string[]> {
  const problems: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') problems.push(`console: ${message.text()}`);
  });
  page.on('pageerror', (error) => problems.push(`page: ${error.message}`));
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (event) => {
      console.error(`CSP blocked ${event.violatedDirective}: ${event.blockedURI || 'inline'}`);
    });
  });
  await page.goto(`/${hash}`);
  await page.getByRole('heading', { level: 1 }).waitFor();
  await page.evaluate(() => document.fonts.ready);
  return problems;
}

/** Interactions that put each page in the state worth a baseline. */
async function prepare(page: Page, name: string) {
  if (name === 'chats') await page.getByRole('button', { name: /Book club/ }).click();
  await page.mouse.move(0, 0);
}

for (const theme of THEMES) {
  for (const size of WIDTHS) {
    for (const target of PAGES) {
      test(`${target.name} at ${size.name} width, ${theme}`, async ({ page }) => {
        await page.setViewportSize({ width: size.width, height: size.height });
        await page.emulateMedia({ colorScheme: theme });
        const problems = await open(page, target.hash);
        await prepare(page, target.name);
        // Grow the viewport to the whole page rather than using fullPage: a full-page capture pins the
        // sticky tab bar and rail to the first screen, which no one ever sees.
        const height = await page.evaluate(() => document.documentElement.scrollHeight);
        await page.setViewportSize({ width: size.width, height: Math.max(height, size.height) });
        await expect(page).toHaveScreenshot(`${target.name}-${size.name}-${theme}.png`);
        expect(problems).toEqual([]);
      });
    }
  }
}

test.describe('accessibility', () => {
  for (const theme of THEMES) {
    for (const target of PAGES) {
      test(`${target.name} has no axe violations, ${theme}`, async ({ page }) => {
        await page.emulateMedia({ colorScheme: theme });
        await open(page, target.hash);
        await prepare(page, target.name);
        // evaluate() runs outside the page's CSP, so axe needs no script tag.
        await page.evaluate(AXE);
        const violations = await page.evaluate(async () => {
          const axe = (window as unknown as { axe: { run: () => Promise<{ violations: { id: string; nodes: { target: string[] }[] }[] }> } }).axe;
          const result = await axe.run();
          return result.violations.map((item) => `${item.id}: ${item.nodes.map((node) => node.target.join(' ')).join(', ')}`);
        });
        expect(violations).toEqual([]);
      });
    }
  }
});

test('the app shows a way home for an unknown route', async ({ page }) => {
  const problems = await open(page, '#/nowhere');
  await expect(page.getByText("There's no page here.")).toBeVisible();
  await page.getByRole('link', { name: 'Go to Home' }).click();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText("Dom's online");
  expect(problems).toEqual([]);
});

test('a slider change and a confirmed leave both report back', async ({ page }) => {
  await open(page, '#/chats');
  await page.getByRole('button', { name: /Sunday league/ }).click();
  const sarcasm = page.getByRole('slider', { name: 'Sarcasm' });
  await sarcasm.focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('status').getByText('Sarcasm set to 55 in Sunday league')).toBeVisible();

  await page.getByRole('button', { name: 'Leave group' }).click();
  const dialog = page.getByRole('dialog', { name: 'Leave Sunday league?' });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Leave group' }).click();
  await expect(page.getByRole('status').getByText('Left Sunday league')).toBeVisible();
  await expect(page.getByRole('button', { name: /Sunday league/ })).toHaveCount(0);
});

test('testing a model shows the result, and a missing key says how to fix it', async ({ page }) => {
  await open(page, '#/settings');
  await page.getByRole('button', { name: 'Test Drafting model' }).click();
  await expect(page.getByText('ANTHROPIC_API_KEY is not set in .env')).toBeVisible();
  await expect(page.getByText("Add ANTHROPIC_API_KEY to Dom's .env, then restart him.")).toBeVisible();
  const memory = page.getByRole('switch', { name: 'Let Dom edit his own memory' });
  await expect(memory).toHaveAttribute('aria-checked', 'false');
  await memory.click();
  await expect(memory).toHaveAttribute('aria-checked', 'true');
});
