# Roadmap

web-qa is an autonomous web-app QA skill for AI coding agents. This is a living
document describing current direction — not a set of promises. Issues and
feedback are welcome and will shape what moves up.

_Last updated: 2026-07_

## Shipped — v0.3.6 (plugin distribution)

- **Plugin-cache-safe layout** — `SKILL.md` moved to `skills/web-qa/`, every command
  referenced as `${CLAUDE_PLUGIN_ROOT}/bin/web-qa-*` with a git-clone fallback, so
  plugin installs resolve tooling deterministically (no reliance on `bin/` being on PATH)
- **Sister skill published** — [tg-qa](https://github.com/c-c0rtex/tg-qa) (autonomous
  Telegram-bot QA) joins the `c-c0rtex` marketplace alongside web-qa

## Shipped — v0.3.5 (reliability & adoption)

- **Live locator probe** — generated locators are verified against the RUNNING app before
  a spec is accepted: entry-page misses and strict-mode ambiguities earn one regeneration
  retry with real-DOM feedback, the rest surface as `probe_warnings` (`live_probe: false`
  / `--no-probe` to skip)
- **Seeded fixtures** — `fixture_cmd` / `fixture_teardown_cmd` seed deterministic data
  before runs (once per matrix); makes visual regression viable on data-driven pages
- **Network assertions** — a 5xx during a passive TC's navigation fails that TC
  (per-TC attribution, `network_fail_on`)
- **Baseline review workflow** — visual regressions save a baseline/current/diff-mask
  triple, land in a report review queue, and are accepted selectively with
  `--update-baseline --routes '<glob>'`
- **CI-native reports** — `matrix --junit <path>` (JUnit XML) + automatic
  `$GITHUB_STEP_SUMMARY` append inside GitHub Actions
- **Healing input upgrade** — the fix prompt embeds playwright's `error-context.md`
  (ARIA snapshot of the failure moment); `--with-screens` hands failure screenshots
  to the orchestrating agent

## Shipped — v0.3.4 (dogfooding on RealWorld)

Everything here came out of building [web-qa-demo](https://github.com/c-c0rtex/web-qa-demo)
against a real RealWorld stack:

- **Config-driven auth adapter** — `auth_login_path` / `auth_login_body` /
  `auth_token_field` / `auth_browser_storage`: the app's login contract is declared in
  config.json instead of hardcoded; SPAs that keep the JWT in localStorage get it planted
  into Playwright storageState
- **Route mining requires framework evidence** — `app/`/`pages/` only count as file
  routers when next/nuxt/@sveltejs/kit is in package.json; a plain React `src/pages`
  layer (FSD) no longer floods the map with junk routes
- **Crawler honesty** — SPA auth redirects recorded as `redirects to /` instead of
  duplicated rows; bare `/` and query-only paths (`/?limit=10`) extractable from TCs
- **Preflight depth** — doctor verifies device-viewport engines (iPhone → webkit
  installed?); new `workers` config key; register-project scaffolds a clean null-keyed
  config.json (no more registry fields leaking into project repos)

## Shipped — v0.3.3 (map completeness & plugin distribution)

- **Language-agnostic TC classification** — the declared `**Type:**` field is the single
  source of truth (structural evidence in `**Steps:**` overrides a mislabeled `passive`);
  prose keyword matching removed entirely, TCs work identically in any language
- **Static route mining** — routes declared in the app's source (Next/Nuxt/SvelteKit file
  routers, router configs) seed the crawl and expand the coverage denominator; new `Origin`
  column in the Routes table, `frontend_dir` config key for monorepos
- **`--interactive` crawl pass (opt-in)** — clicks through runtime-only navigation
  (pushState buttons without `<a href>`); mutation safety enforced at the network level:
  all non-GET requests aborted during the pass
- **Claude Code plugin** — installable via `/plugin marketplace add c-c0rtex/web-qa` →
  `/plugin install web-qa@c-c0rtex`; registry moves to `~/.config/web-qa/projects.json`
  for plugin installs so it survives updates

## Shipped — v0.3.2 (industry best practices)

- **Seed spec grounding** — optional committed `.web-qa/seed.spec.ts` (human-verified
  auth/setup code) is embedded into generation and healing prompts: working code beats a
  prose auth hint
- **ARIA snapshots in the app map** — per-route role/name snapshots (ground truth for
  `getByRole`), selector priority now `getByTestId` → `getByRole` → `getByLabel`/`getByText`
- **Healer classification** — test fragility → fix; transient environment failure → spec
  untouched; genuine app bug → assertions kept, `test.fixme` + `BUGS.md` entry (healers patch
  test fragility, never real bugs)
- **Config hygiene** — `retries: CI ? 2 : 0`, `trace: 'on-first-retry'`, mock-external-only rule

## Shipped — v0.3.1 (RBAC-aware generation)

- **UI-first rule** — generated and healed specs must drive user steps through the real UI;
  `page.request` is confined to auth, setup/teardown and side-verification (an API-rerouted
  test that passes while the UI is broken is a spec bug, and now the prompts say so)
- **Role-annotated test cases** — `**Role:** viewer` in a TC: the passive runner skips it
  under other accounts, the matrix schedules it only into matching role combos, and spec
  generation logs in with that role's credentials
- **RBAC directive in scenario generation** — a diff touching permissions/roles/scopes fans
  out into allowed-path + denied-path TC pairs per project role (UI control hidden AND direct
  access rejected)

## Shipped — v0.3.0 (responsive & RBAC depth)

- **Viewports as a matrix dimension** — config `viewports` (named sizes or Playwright device
  descriptors: touch, mobile UA, DPR); `--viewport` on explore/run, `--viewports` on the matrix
  (crossed with `--roles`); per-viewport visual baselines (`@name` suffix, default stays
  unsuffixed)
- **Mobile-aware exploration** — `web-qa-explore --viewport mobile` writes
  `app.context.<name>.md`; spec generation reads all maps, so mobile TCs target the mobile DOM
- **Specs under mobile emulation** — a device viewport adds a `mobile` playwright project
  (`WEBQA_MOBILE_DEVICE`) next to chromium
- **Route coverage × roles** — the matrix reports per-role uncovered routes
  ("this route was never tested as `viewer`")

## Shipped — v0.2.0 (hardening)

- **Manual-edit-safe app map** — everything below the `<!-- manual -->` marker in
  `app.context.md` survives `web-qa-explore` re-crawls; the marker is scaffolded on first write
- **`web-qa-doctor`** — one-command preflight: deps, chromium build, LLM CLI, registry,
  project path/config, frontend/backend reachability, login (including every role),
  app map / scenarios / specs-runner setup. Exit 0/1
- **Configurable viewport** — per-project `viewport` config applied consistently to the
  crawler, the passive runner and the specs config (`WEBQA_VIEWPORT`); default unified
  at 1280×900 across all layers (previously hardcoded and inconsistent)

## Shipped — v0.1.0

- **Explore once** — crawls the app into a cached map (`app.context.md`)
- **Grounded spec generation** — Playwright E2E specs written against the real crawled map, not guesses
- **Zero-token runs** — the run phase is pure Playwright; no LLM in the loop
- **Visual regression** — algorithmic pixel diff against committed baselines (no vision model looks at your screenshots)
- **Accessibility** — axe-core checks as part of the suite
- **Self-healing** — failing specs get a proposed fix (applied by opt-in, rolled back if unparseable)
- **Deploy gate** — `web-qa-matrix`: full inventory, route coverage, flaky detection, exit code for CI
- **CI** — GitHub Actions: ruff lint + unit tests + install/CLI smoke

## Next

### v0.4 — cross-agent & smarter healing

- [ ] **Cross-agent support** — decouple generation from the `claude -p` binary behind a
  configurable command (`WEBQA_LLM_CMD`), so the same skill runs under Codex CLI, Gemini CLI,
  Kimi Code CLI. The deterministic run phase is already agent-agnostic. Ships with a
  compatibility matrix and installs under the shared `.agents/skills/` path
- [ ] **Healing v2** — flake-aware: tell transient failures (network timeouts, rate limits,
  races) apart from real regressions — retry the former, heal the latter. (The other half —
  `error-context.md` ARIA snapshots in the healing prompt — shipped in v0.3.5)
- [ ] **Parallel passive stage** — run passive TCs across multiple pages; needs care on
  small dev stands where parallel browsers turn timing into flaky noise (`workers: 1` exists
  for a reason)

## Exploring

Directional. Shape may change; not committed.

- Richer assertions beyond DOM / pixel / a11y / network — console-error gating,
  performance budgets
- API testing from OpenAPI — a deterministic contract layer (schemathesis integration,
  zero tokens) + LLM-generated API flow scenarios (`web-qa-generate --api`), endpoint
  coverage in the matrix
- Single-file HTML report with diff artifacts inline `help wanted`

## Out of scope (for now)

- Being a general browser-automation agent — web-qa is a QA runner, not a driver
- Sending screenshots to a vision model **during the run phase** — runs stay zero-token and
  deterministic by design
