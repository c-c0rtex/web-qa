/**
 * Template playwright.config.ts for <project>/.web-qa/
 *
 * Copy to <project>/.web-qa/playwright.config.ts. The stand's URL comes from the
 * registry at run time (WEBQA_BASE_URL); nothing here is project-specific.
 * Used by `web-qa-run-specs --alias <a>`.
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
  // WEBQA_OUTPUT_DIR is set by web-qa-matrix / web-qa-maintain to that run's own folder.
  // Playwright DELETES its output dir at the start of every run, so a shared default means
  // run N+1 destroys run N's screenshots, traces and error-context page snapshots — the only
  // artifacts that explain a failure after the fact. One run, one folder.
  outputDir: process.env.WEBQA_OUTPUT_DIR ?? 'test-results',
  fullyParallel: false,
  // WEBQA_WORKERS=4 is safe for read-only suites; keep 1 when mutating specs share backend state
  workers: Number(process.env.WEBQA_WORKERS ?? 1),
  reporter: [['list'], ['json', { outputFile: 'reports/playwright-results.json' }]],
  // Whole-test budget. Deliberately generous: a genuinely slow step (file upload, async
  // parse) legitimately waits ~30s, and when the test budget was 30s such a step killed the
  // test from the outside — producing a bare "Test timeout of 30000ms exceeded." that names
  // no locator and no step. Fast failure is bought by actionTimeout/navigationTimeout below,
  // which bound each individual operation; the test budget only needs to bound the whole.
  // spec-gen reads this value and forbids generated waits from exceeding it.
  timeout: Number(process.env.WEBQA_TEST_TIMEOUT ?? 60_000),
  expect: { timeout: 8_000 },
  // 2 retries in CI separate transient flakes from real failures; 0 locally for fast feedback
  retries: process.env.CI ? 2 : 0,
  use: {
    // The stand is not the spec's business: web-qa-matrix / -run-specs / -maintain set
    // WEBQA_BASE_URL from the registry (a caller's own value wins — that is how CI retargets),
    // and generated specs navigate with relative paths against it.
    baseURL: process.env.WEBQA_BASE_URL ?? 'http://127.0.0.1:3000',
    // Without this, a locator that never matches makes `click`/`fill` wait until the
    // 30s TEST timeout, and playwright reports a bare "Test timeout of 30000ms
    // exceeded." with no locator, no step, nothing. Bounding the ACTION instead
    // turns the same failure into "locator not found: getByLabel(/Price/i)".
    actionTimeout: 10_000,
    // 30s, not 15s: a dev server compiles a route on its first request, and under a serial
    // 70-spec run that cold compile regularly crosses 15s. A genuinely hung navigation still
    // dies on the whole-test budget above, so nothing is lost but false failures.
    navigationTimeout: 30_000,
    // CI: trace on retry only; locally there are no retries — keep traces for failures
    trace: process.env.CI ? 'on-first-retry' : 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    headless: true,
  },
  projects: [
    // viewport must live at PROJECT level: the Desktop Chrome descriptor carries its own
    // viewport (1280x720) which would silently override the top-level `use.viewport`
    { name: 'chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: vpWidth, height: vpHeight } } },
    // WEBQA_MOBILE_DEVICE ("iPhone 14") is set by web-qa-matrix when a device-viewport
    // is requested (--viewports incl. a `device` entry) — specs then run under real
    // mobile emulation (touch, UA, DPR) as a second project.
    ...(process.env.WEBQA_MOBILE_DEVICE
      ? [{ name: 'mobile', use: { ...devices[process.env.WEBQA_MOBILE_DEVICE] } }]
      : []),
  ],
});
