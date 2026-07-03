/**
 * Template playwright.config.ts for <project>/.web-qa/
 *
 * Copy to <project>/.web-qa/playwright.config.ts and adjust baseURL/viewport
 * to match the project. Used by `web-qa-run-specs --alias <a>`.
 *
 * Setup once per project (NOT `npm init -y` — the ".web-qa" dir name is an
 * invalid npm package name; and a bare `npm install` without a local
 * package.json walks up and pollutes the app's own package.json):
 *   cd <project>/.web-qa
 *   [ -f package.json ] || printf '{"name":"web-qa-specs","private":true}\n' > package.json
 *   npm install -D @playwright/test
 *   npx playwright install chromium
 *   cp ~/.claude/skills/web-qa/playwright.config.template.ts playwright.config.ts
 */
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './specs',
  testMatch: '**/*.spec.ts',
  fullyParallel: false,
  // WEBQA_WORKERS=4 is safe for read-only suites; keep 1 when mutating specs share backend state
  workers: Number(process.env.WEBQA_WORKERS ?? 1),
  reporter: [['list'], ['json', { outputFile: 'reports/playwright-results.json' }]],
  timeout: 30_000,
  expect: { timeout: 8_000 },
  use: {
    baseURL: 'http://127.0.0.1:3000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    headless: true,
    viewport: { width: 1920, height: 1080 },
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
