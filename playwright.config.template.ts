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

// WEBQA_VIEWPORT ("1280x900") is set by web-qa-matrix / web-qa-maintain from the
// project's `viewport` config key, so specs see the same window as the crawler
// and the passive runner. Falls back to the web-qa default.
const [vpWidth, vpHeight] = (process.env.WEBQA_VIEWPORT ?? '1280x900')
  .split('x').map(Number);

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
    viewport: { width: vpWidth, height: vpHeight },
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    // WEBQA_MOBILE_DEVICE ("iPhone 14") is set by web-qa-matrix when a device-viewport
    // is requested (--viewports incl. a `device` entry) — specs then run under real
    // mobile emulation (touch, UA, DPR) as a second project.
    ...(process.env.WEBQA_MOBILE_DEVICE
      ? [{ name: 'mobile', use: { ...devices[process.env.WEBQA_MOBILE_DEVICE] } }]
      : []),
  ],
});
