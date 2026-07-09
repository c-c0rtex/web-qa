# Roadmap

web-qa is an autonomous web-app QA skill for AI coding agents. This is a living
document describing current direction — not a set of promises. Issues and
feedback are welcome and will shape what moves up.

_Last updated: 2026-07_

## Shipped — v0.4.6 (the map was truncated before anyone could see it)

v0.4.5 removed the cap that rationed ARIA snapshots across routes, and the map got no
better. The cap that mattered sat one layer upstream, in the crawler:

```python
summary["aria"] = page.locator("body").aria_snapshot()[:800]
```

Eight hundred characters, applied at capture. On any app with a sidebar, that is the sidebar.
No table, no heading, not one action button ever reached the map — not in any version of this
tool. Raising the render cap could not help, because nothing longer than 800 characters had
ever survived to be rendered. Verified on a real app: every snapshot in the file was exactly
800 characters; after the fix the median is 6 859 and the page whose button the generator had
been guessing with a regex alternation now states its caption outright.

- **One `ARIA_PAGE_MAX`, used where the snapshot is taken and where it is written.** A test
  fails if the two ever drift again. Truncation is recorded at capture, so the renderer can
  report it instead of silently emitting a full-looking snapshot capped to exactly the limit
- **`slice_openapi` dropped the enum block from every prompt.** `### Enum values` is a `###`
  sibling of the endpoint groups and belongs to none of them, so group-filtering removed it —
  while the prompt instructed the model to take allowed values from it. It always rides along now
- **The scenario generator was losing the sections its own rule cites.** It loads the whole map
  and the truncation eats the tail: `Backend endpoints`, `Enum values`, `Auth Flow`. It writes
  prose test cases, not locators, so it now gets the map with the snapshot section dropped
- **The spec cache was keyed on a truncated map**, so a re-crawl that changed anything past
  the cut left every cached spec looking current. The fingerprint is taken from the files on disk
- **`maintain` slices the map by the routes its spec navigates to.** With whole-page snapshots,
  handing it the first 48 000 characters means handing it whichever routes sort first

## Shipped — v0.4.5 (the runners stop guessing what they were never told)

A full run over a real app produced 52 failing specs. Four were candidate product bugs. The
rest were the tool asking the model to invent facts it already had, or had thrown away. Each
item below is one of those failure modes, traced to its cause.

- **The map carried no API contract.** The OpenAPI section listed paths, methods and request
  fields — never the success status code, never the response shape, never an enum's wire
  values. So the generator guessed: `(await r.json()).filter(...)` on a paginated envelope is
  a `TypeError`, a `201` asserted as `200` is a red test against a correct API, and a wire
  enum compared against the label the UI renders for it is wrong even when both sides are
  right. `explore` now mines all three. Deterministic, zero tokens
- **ARIA snapshots were rationed to uselessness.** v0.4.3 shared a 16 KB budget evenly across
  every route; on a 47-route app that is 400 characters each — a page title and two nodes.
  The budget existed because the whole map used to ride in every prompt. It doesn't:
  `slice_aria` sends only the routes of the test case at hand. The cap moved there, and the
  map now stores what was actually captured
- **The generator was never told the test budget.** It wrote `waitFor({timeout: 60000})` into
  a 30-second test. Playwright killed the test from the outside and reported
  `Test timeout of 30000ms exceeded.` — naming no locator and no step. The budget is now
  stated in the prompt, read from the project's own config, and the template's budget is
  long enough for a legitimately slow upload
- **`maintain` never once read the page snapshot it prompts with.** `Path(name).stem` leaves
  `.spec` glued on, so the prefix never matched a single playwright result directory. The
  healer believed it had the failing page's DOM and silently didn't. It does now — and
  `--artifacts-dir` lets it heal from an archived run
- **The failure evidence was destroyed by the next run.** Playwright deletes its output dir
  at the start of every run, and every run shared one. The matrix kept the json and nothing
  else. Each run now owns its `reports/<run-id>/test-results/`, traces included; old ones are
  pruned to `--keep-artifacts` (default 3). `maintain` derives the folder from the report it
  was given, so an old report heals against its own snapshots — and `--reruns`, which used to
  run playwright before the healer read anything, no longer erases what it is about to read
- **The mutation gate covered half the pipeline.** Only the passive runner honoured
  `**Type:** mutating`; the specs stage handed every `.spec.ts` to playwright regardless. A
  run that reported 78 mutating test cases as `✋ manual` had already created, edited and
  deleted rows. Specs are now labelled `spec (mutating)`, and `--no-mutations` means it
- **A report could not say what produced it.** `matrix.json` recorded stats and never argv.
  Whether a run skipped a stage, used two workers, or wrote to the database was
  unreconstructable. It records `invocation` now
- **`api_login` returned the login response and called it the user.** For a JWT app that is
  `{access_token, token_type}`, so every report said `Logged-in: None (None)` and `doctor`'s
  per-role check proved only that the password was right — never that the account holds the
  role the RBAC test cases run under. It reads the identity endpoint (`auth_me_path`)
- **The oracle rule created false positives.** "Recompute the metric from primary
  collections" makes the spec a second, unverified implementation of the thing it checks —
  count the wrong entity and the test is red while the app is right. Both prompts now prefer
  drill-down (assert the summary against the detail view the app itself renders) and
  mutation deltas; recomputation is a last resort that must cite the definition and state its
  unit. `maintain` will not call a recomputed mismatch a product bug, and knows that a
  `[disabled]` control is not a permission leak
- **Screenshots of failures were blank.** Specs queried the API before the first `page.goto`,
  so the artifact a human needs was `about:blank`. The prompt opens the page first
- **`doctor` checks the config against the template's invariants.** The template is copied
  once at setup and every later fix is absent from every existing project — silently. A
  `use.viewport` shadowed by a device descriptor, a missing `actionTimeout`, a test budget
  shorter than a slow upload: all reported, with the template to re-copy
- Also: unlabeled inputs and non-unique names are named in the prompt as what they are — an
  app defect and a scoping mistake — instead of being met with a locator that cannot match;
  and generated specs no longer invent credentials or fixture paths

## Shipped — v0.4.4 (examples belong to nobody)

Docs, prompt examples and tests quoted routes, button captions, KPI names and enum labels
taken verbatim from the closed codebase web-qa was being debugged against. None of it was
needed to explain a change to this tool. Findings are now stated as properties of web-qa,
every example is a neutral placeholder, and the history was rewritten to match. No
behaviour changed.

## Shipped — v0.4.3 (the app map stops lying, and says so when it fails)

Every spec that filled a form hung for 30 seconds and reported only `Test timeout of 30000ms
exceeded.` — no locator, no step. The chain, verified end to end: the ARIA budget was spent
in crawl order, so the later routes got no snapshot; the prompt demands map-grounded
selectors and the map had none, so the model guessed the button caption with a regex
alternation; the real caption was none of them; and with no `actionTimeout` the miss hung
until the test timeout, silently. The timeouts clustered almost entirely on routes with no
snapshot.

- **`actionTimeout` in the config template** — the same failure now names the locator.
  `doctor` warns when a project's config lacks it, since the template does not back-propagate
- **The ARIA budget is shared evenly**, not first-come. The first ten routes crawled got a
  full snapshot and every route after them — often the most-tested sections — got none
- **The crawler stopped following screenshots.** A docs page linking to 40 PNGs added 40
  "routes", half the Routes table, and took 40 of the 74 snapshot slots. Paths are canonical
  too: an SPA that replaceStates `?loaded=14` onto a list page no longer registers a second route
- **`explore` merges instead of overwriting.** A lower `--max-pages`, an expired session or
  one slow page silently replaced a good map with a worse one. Routes now merge against an
  `app.context.json` sidecar; unreached ones are carried over and marked stale, a vanished
  snapshot is reported as a REGRESSION, `--fresh` restores the old behaviour, and a
  `--max-pages` below the mined route count is a warning, not a surprise
- **The map speaks in templates** — one row per route, naming the page each snapshot was
  sampled from. It no longer advertises `/items/23` two lines after the prompt forbade
  hardcoding ids
- **The prompt carries only what the TC can use.** ARIA and OpenAPI are sliced to the routes
  a test case visits and the endpoints it names; the hand-written manual section is protected
  from truncation instead of being the first thing dropped. On a 40 KB map a single-route
  spec now sees 9 KB where it used to see a 24 KB truncation
- One matrix run leaves one folder: `reports/<id>/` with `matrix/` nested inside it
- The registry left the skill root, which shadowed the user's real one, and now holds only
  `path`/`auth`/`roles`; URLs moved to the project's committable `.web-qa/config.json`

## Shipped — v0.4.2 (specs that test data, not decoration)

A generated spec asserted that a dashboard's KPI tiles *rendered*. A tile that renders `0`
renders. The test was green and said nothing about whether the number was right. Presence is
not correctness, and the prompts asked for presence.

- **Independent-oracle rule** — a TC over derived data (a KPI, a total, a count, a ranking)
  must now compute the expected value from the backend's **primary** collections and assert
  the UI equals it. Deriving it from the same aggregate endpoint the page calls is explicitly
  forbidden: when that aggregation is the bug, UI and oracle are wrong together and the test
  passes on a broken feature
- **`page.request` is no longer barred from oracle reads.** The UI-FIRST rule allowed API
  calls only for auth, fixtures, and post-action verification — so on a read-only page the
  model correctly concluded it must not check the numbers at all
- **Presence assertions no longer count** — `toBeVisible` does not cover an Expected bullet
  that names a value; the spec must assert the value
- Known limit, found while validating this: an oracle that *re-implements* the metric is a
  second, unverified implementation, written by a model that never saw the definition. A
  regenerated spec duly failed — and the application was right. It counted rows of one entity
  where the metric counts rows of another, and compared an API enum value against the
  human-readable label the UI renders for it. A failing data assertion is a question, not a
  verdict; prefer an oracle that does not re-implement anything (a drill-down list, a
  round-trip, a mutation invariant). See v0.4.3

Reporting stopped lying about what happened:

- **A budget stop is not a spec failure** — TCs blocked by `LLMBudgetExceeded` never reached
  the model, so they no longer get a `.FAILED` marker (which means "the generator wrote a bad
  spec"). They land in `skipped_over_budget`, with a resume command
- **A missing role is not an error** — a TC declaring a role absent from `projects.json` costs
  nothing and is fixed by a human; it moved out of `errors` into `skipped_missing_role`
- **Cost is projected early** — the run warns as soon as the extrapolated total exceeds the
  ceiling, instead of running out on the last test case of twenty

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
