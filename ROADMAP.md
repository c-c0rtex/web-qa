# Roadmap

web-qa is an autonomous web-app QA skill for AI coding agents. This is a living
document describing current direction — not a set of promises. Issues and
feedback are welcome and will shape what moves up.

_Last updated: 2026-07_

## Shipped — v0.4.1 (bounded token spend)

Every LLM call web-qa makes is a headless `claude -p` session, not a single API request.
Left at the CLI's own defaults, a 30-test-case fan-out drained a 5-hour subscription
window in under seven minutes. The generator is now cheap and bounded by construction:

- **Tools off by default** (`WEBQA_CLAUDE_TOOLS=""`) — `app.context.md` is already inlined
  in the prompt, so the agentic loop that re-crawled the repo bought nothing and cost the
  most. This is the single largest saving
- **`sonnet` + `medium` effort by default** — spec generation is a mechanical translation;
  it no longer inherits whatever frontier model the user's interactive CLI is pinned to
- **Real spend metering and a hard ceiling** — `--output-format json` yields the CLI's own
  `total_cost_usd`; `WEBQA_MAX_USD` (default $5, `--max-usd` per run, `0` disables) raises
  `LLMBudgetExceeded` *before* the next call reaches the model. Every runner's summary JSON
  now carries `"llm": {spent_usd, calls, budget_usd}`
- **Serial by default** (`--workers 1`) — concurrent `claude -p` calls all miss the shared
  prompt cache, since none can read what the others are still writing
- **Usage-limit failures are legible** — the CLI reports exhaustion on stdout, which the
  error path discarded; `*.FAILED` markers said only `exit 1`

Scenario generation also stopped being coverage-blind:

- **Deterministic route coverage** — `generate` now computes which app-map routes no test
  case's `**Steps:**` navigate to, feeds that to the prompt, returns `uncovered_routes` in
  its summary and prints a loud `COVERAGE GAP` warning. A model answering one scoped task
  ("orders regression") cannot know an untouched dashboard exists; nothing else would ever
  say so. `--cover-gaps` generates test cases for exactly those routes
- **Model tiers follow the job** — deciding *what* to test is judgment and runs once per
  invocation, so `generate` uses `opus`/`high`; turning a decided test case into code is
  translation fanned out over every TC, so `spec-gen` stays on `sonnet`/`medium`

## Shipped — v0.4.0 (signal quality & drag-and-drop)

- **Flake quarantine** — `quarantine_after: N`: a spec that flips pass/fail across the
  last N matrix runs is auto-quarantined (🚧) — still executed and reported, but kept out
  of the gate's exit code and emitted as `skipped` in JUnit until it stabilizes. Default
  off; the data was already in `history.json`
- **Flake-aware healing (deterministic-first)** — before spending an LLM heal, maintain
  classifies transients WITHOUT a model: a network/infra/rate-limit signature in the
  failure (`net::ERR_*`, `ECONNRESET`, 429, 50x, `Target closed`…) is retried, not healed;
  `--reruns N` additionally re-runs a signature-clean spec and treats a pass as a flake.
  Only consistently-failing specs reach the healer
- **Console-error gating** — `console_fail_on: ["error"]` (opt-in) fails a passive TC that
  throws to the console during its navigation; `console_ignore` regexes drop known noise.
  Another zero-token oracle signal
- **Drag-and-drop recipes** — the spec generator detects the app's DnD library from
  package.json (dnd-kit, react-beautiful-dnd / @hello-pangea, SortableJS, interact.js,
  native HTML5) and injects the exact, proven `dragTo` helper for it — the multi-step
  pointer sequence that trips pointer-sensor activation thresholds, where a single
  high-level `dragAndDrop` silently no-ops. Assertions go through DOM order/containment,
  not visual diff

## Shipped — v0.3.6 (plugin distribution)

- **Plugin-cache-safe layout** — `SKILL.md` moved to `skills/web-qa/`, every command
  referenced as `${CLAUDE_PLUGIN_ROOT}/bin/web-qa-*` with a git-clone fallback, so
  plugin installs resolve tooling deterministically (no reliance on `bin/` being on PATH)
- **Sister skill published** — [tg-qa](https://codeberg.org/c-c0rtex/tg-qa) (autonomous
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

Everything here came out of building [web-qa-demo](https://codeberg.org/c-c0rtex/web-qa-demo)
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

Ordered by value-to-effort, not by size of idea. web-qa and tg-qa now share the same
core (`call_claude`, registry, matrix, maintain), so the cross-cutting items below —
cross-agent, quarantine, flake-aware healing — are meant to be designed **once in the
shared layer** and land in both skills, not reimplemented twice.

### v0.5 — cross-agent (portability)

- [ ] **Cross-agent support (MVP first)** — decouple generation from the `claude -p`
  binary behind a configurable command (`WEBQA_LLM_CMD`), extending the run phase's
  agent-agnosticism to generation. The hard part isn't the env var — it's normalizing
  output framing, flags and timeouts across CLIs. So the MVP is the command indirection +
  **one** proven alternate CLI (Codex); the full compatibility matrix (Gemini, Kimi, …)
  and the shared `.agents/skills/` install path grow from there rather than gating the
  release. Biggest single adoption lever, so it gets its own release

### v0.6+ — demand-gated

Real designs, but they solve problems that only appear at scale. Sequenced by actual
demand signal, not the calendar.

- [ ] **Matrix sharding** — `web-qa-matrix --shard i/n` splits the whole inventory
  (passive TCs + specs) across CI runners and merges the per-shard JUnit into one gate
  verdict; Playwright's own `--shard` covers only the spec stage, the passive stage needs
  its own split. For suites a single-runner gate has outgrown
- [ ] **Parallel passive stage** — run passive TCs across multiple pages, behind explicit
  opt-in + per-stand concurrency detection. Small dev stands turn parallel browsers into
  flaky noise (`workers: 1` exists for a reason), so this waits until quarantine +
  flake-aware healing are in to absorb the risk it adds
- [ ] **Incremental re-explore** — `web-qa-explore --diff <ref>` re-crawls only the routes
  a diff touches (changed frontend files → owning routes via the mining map) and merges
  them into `app.context.md`. The mapping stays conservative: a shared component/layout
  change re-crawls dependent routes rather than missing them. Delivers the architecture's
  "incrementally after" promise

## Exploring

Directional. Shape may change; not committed.

- **OAuth / SSO login flows** — segment-gated, but the real gate between "works on demo
  apps" and "works on my company's app": the config auth adapter plants a token from a
  direct login endpoint; third-party IdP redirects (OAuth authorization-code, SAML) still
  need a scripted consent pass, driven once and cached into `storageState`
- **Per-locale runs** — locale as a matrix dimension (like roles / viewports —
  architecturally cheap since the dimension machinery exists), with per-locale visual
  baselines for i18n layout regressions
- **Performance budgets** — a timing/resource oracle alongside the console-error gating above
- **Single-file HTML report** with diff artifacts inline `help wanted`

## Elsewhere, not here

- **API testing from OpenAPI** — a deterministic contract layer (schemathesis, zero
  tokens) + LLM-generated API flow scenarios is a different surface with a different
  oracle. It belongs in a dedicated **api-qa** skill (already anticipated in the
  `c-c0rtex` marketplace), not bolted onto web-qa

## Out of scope (for now)

- Being a general browser-automation agent — web-qa is a QA runner, not a driver
- Sending screenshots to a vision model **during the run phase** — runs stay zero-token and
  deterministic by design
