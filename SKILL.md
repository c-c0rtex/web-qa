---
name: web-qa
description: Autonomous web app QA — Playwright E2E + visual regression + axe-core accessibility, with auto-generated test scenarios from git diff or text instructions. Project-portable (each project owns its `.web-qa/` state). Use when the user asks to test a website, verify a frontend feature, run regressions, or audit a11y.
---

# Web QA

Agent skill for testing web applications: E2E + visual regression + a11y + scenario auto-generation. Architecture — a 4-stage sequential pipeline, portable (one skill, many projects).

## When to use

- "Run the regression on /cart", "check that checkout works" — execute existing/new scenarios
- "Generate tests from the diff" — autogen from git changes
- "What's broken in this branch?" — full Exploration + Generation + Run
- pre-push / pre-deploy gate — `web-qa-matrix` runs everything and returns an exit code

## User intent → workflow (recognize these)

| User says (any phrasing) | Do |
|---|---|
| "set up / onboard web-qa here" | Onboarding workflow below (register → creds → doctor → explore → specs runner) |
| "is it healthy / why is it broken" | `web-qa-doctor --alias <a>`, explain failures, apply hints |
| "test <feature>" / "check that <flow> works" | `generate --task` → show the md plan → run passive and/or spec-gen+run |
| "did my branch/diff break anything" | `generate --diff <ref>` → spec-gen → matrix → explain failures |
| "full regression / can I deploy" | `web-qa-matrix` → report gate verdict, coverage, flaky |
| "I changed permissions/roles — verify" | `generate --diff` (RBAC directive fires) → matrix `--roles <all>` — never test just one role |
| "test the mobile version" | `explore --viewport mobile` if no mobile map yet → generate → `run --viewport mobile` / matrix `--viewports` |
| "this test is failing, fix it" | `maintain` (propose); `--apply` only with explicit user consent |
| "the UI change is intentional" | `run --update-baseline` |
| "show me page X / verify visually" | screenshot → Read → describe (see Screenshots section) |

## Architecture

```
   git diff / instruction
            │
            ▼
┌─ Exploration ─┐  read-only crawl of the app → app.context.md
│   (once at onboarding, incrementally after)
└────┬──────────┘
     ▼
┌─ TestCase Gen ─┐  context + diff/task → md scenarios (human checkpoint optional)
└────┬───────────┘
     ▼
┌─ Automation ───┐  md → .spec.ts via `claude -p` CLI (≈4× cheaper than MCP-driving a browser)
└────┬───────────┘
     ▼
┌─ Run ──────────┐  npx playwright test → report
│   + axe-core injection → a11y violations
│   + pixel-diff vs baseline → visual regression
└────┬───────────┘
     ▼
┌─ Maintenance ──┐  failing spec + real error output → PROPOSES a fix (not auto-apply)
└────────────────┘
```

**Key principles:**
- Stages are **sequential**, hand-off through files (not shared memory) — allows pause/edit between phases and cheap context
- Exploration is the most expensive stage; cached in the repo as `<project>/.web-qa/app.context.md`
- Spec generation is grounded in the crawled app map — selectors come from real button labels/routes, not guesses
- Maintenance **proposes a diff**, never auto-fixes silently — distinguishing "the feature changed" from "a real regression" is a human/agent call

## Layout

```
<skill root>/                             # shared infrastructure
├── SKILL.md
├── projects.json                         # machine-local registry (gitignored; see projects.example.json)
├── playwright.config.template.ts         # per-project config template
├── bin/                                  # thin wrappers over runners/ (uv run)
│   ├── web-qa-register-project           # onboard a project (+ .web-qa/ scaffold)
│   ├── web-qa-resolve-project            # cwd → project record
│   ├── web-qa-explore                    # crawl → app.context.md
│   ├── web-qa-generate                   # git diff / task text → scenarios/*.md
│   ├── web-qa-run                        # passive scenario runner (+a11y +visual)
│   ├── web-qa-spec-gen                   # md TC → .spec.ts via claude -p
│   ├── web-qa-run-specs                  # npx playwright test over specs/
│   ├── web-qa-matrix                     # deploy gate: full inventory + run
│   ├── web-qa-maintain                   # self-heal failing specs
│   └── web-qa-kill                       # kill orphaned playwright processes
└── runners/                              # python modules (run via bin/)

<project>/.web-qa/                        # per-project state (lives in the project repo)
├── config.json                           # ALL project specifics (see config.example.json)
├── seed.spec.ts                          # OPTIONAL: human-verified auth/setup code — spec-gen and maintain reuse its patterns verbatim (strongest auth grounding)
├── package.json                          # isolated npm root for @playwright/test
├── app.context.md                        # crawled app map (Exploration writes)
├── scenarios/                            # *.md test cases (generated or hand-written)
├── specs/                                # generated .spec.ts (`_`-prefixed = ad-hoc, excluded from gate)
├── baseline/                             # golden screenshots (commit to git)
├── history.json                          # last 20 matrix runs (flaky detection)
├── reports/                              # per-run artifacts (gitignore)
└── BUGS.md                               # confirmed findings
```

## Configuration

Shared runners, project specifics in two places:

- **`projects.json`** (machine-local, NOT in any repo). Resolution order: `WEBQA_REGISTRY` env → `<skill root>/projects.json` (git-clone install) → `~/.config/web-qa/projects.json` (plugin install — the plugin cache dir changes on every update, so the registry lives outside it; `register-project` picks the right location automatically). Fields: `alias`, `path`, `target_url`, `backend_url`, **`auth: {email, password}`** — credentials live only here. No hardcoded fallbacks: without auth (or `--email/--password`) runners exit with a clear error. Optional **`roles: [{name, email, password}]`** — named accounts for RBAC runs (`--role manager`, `web-qa-matrix --roles admin,manager`). TCs annotated `**Role:** <name>` in scenarios run ONLY under that role's combos (spec-gen also logs them in as that role); unannotated TCs are role-agnostic.
- **`<project>/.web-qa/config.json`** (in the project repo): merged over the registry entry (`null` values ignored). Keys:
  - `stack` — string for the spec-gen prompt (e.g. `"Next.js + FastAPI admin"`, default `"web"`)
  - `backend_prefixes` — paths treated as backend-only (never opened as frontend routes). Default: `/auth`, `/api`, `/health`
  - `route_hints` — `[{path, keywords}]` to infer the route from TC text when no explicit path. Default: empty
  - `id_discovery` — `[{endpoint, key}]`: which GET endpoint to sample a live id from, substituted into `{key}`/`{key_id}`/`{id}` placeholders. Default: empty
  - `auth_flow_notes` — lines for the Auth Flow section of `app.context.md` (otherwise derived from OpenAPI)
  - `auth_login_hint` — exact auth flow description for the spec-gen prompt (JWT vs cookie, browser vs Node-side API). Critical: without it the model guesses the contract. Default: generic hint
  - `auth_login_path` / `auth_login_body` / `auth_token_field` / `auth_browser_storage` — declarative auth adapter for the RUNNERS (explore/run/doctor login): endpoint path (default `/auth/login`), JSON body template with `{email}`/`{password}` (default flat), dot-path to a bearer token in the response (e.g. `"user.token"`), and where the SPA keeps it in the browser (`{"kind": "localStorage", "key": "...", "value": "{token}"}` → injected into Playwright storageState). Cookie-session apps need none of these. Example (RealWorld): path `/api/users/login`, body `{"user": {"email": "{email}", "password": "{password}"}}`
  - `context_notes` — lines for the Out of Scope / Notes section of `app.context.md`
  - `visual_masks` — CSS selectors of dynamic elements (clocks, counters, avatars) hidden before screenshots to avoid false visual diffs. Default: empty
  - `visual_exclude` — route globs fully excluded from visual diffing (data-driven pages: entity lists, dashboards — their baseline rots with every data change). Screenshot still saved as an artifact. Default: empty. Rule of thumb: point dynamics → mask, whole-page data → exclude; the fundamental fix is seeded fixture data
  - `test_data_prefix` — name prefix for test entities that mutating specs create/delete themselves (policy: never touch real data). Default: `"QA-"`
  - `gate_exclude` — spec globs excluded from the deploy matrix (features hidden on prod behind flags/build-args), e.g. `["analytics*"]`. Default: empty
  - `language` — language for generated scenario steps (default `"English"`)
  - `frontend_dir` — path (relative to the project root) of the frontend app inside a monorepo; route mining scans there. Default: project root
  - `workers` — Playwright workers for matrix/maintain runs. Set `1` for small dev stands: parallel chromiums against one dev server turn timing into flaky noise. CLI `--workers` overrides. Default: template default
  - `live_probe` — `false` disables the live locator check during spec-gen/maintain (e.g. generating without a running stand). Default: enabled. The probe opens each generated spec's entry page (authenticated) and counts matches for every static locator; misses and strict-mode ambiguities (>1 match) earn one regeneration retry with real-DOM feedback, then surface as `probe_warnings` — never a hard failure, since mid-flow elements legitimately don't exist on the entry page
  - `viewport` — `{"width": W, "height": H}`, applied consistently to the crawler, the passive runner AND the specs config (via `WEBQA_VIEWPORT`, set automatically by matrix/maintain). Default: 1280×900 everywhere. Changing it invalidates visual baselines (size-mismatch) — re-run `--update-baseline` after
  - `viewports` — named list for responsive testing: `[{"name": "desktop", "width": 1280, "height": 900}, {"name": "mobile", "device": "iPhone 14"}]`. `device` entries use full Playwright descriptors (touch, mobile UA, DPR) — real emulation, not a narrow window. First entry = project default (keeps unsuffixed baselines); others get their own baseline set (`<route>@<name>.png`). Used by `--viewport` (explore/run) and `--viewports` (matrix)

Env: `WEBQA_CLAUDE_MODEL=sonnet|opus` — model for internal `claude -p` calls (spec-gen / generate / maintain); defaults to the CLI default. `WEBQA_WORKERS=N` — Playwright workers in the template config. `WEBQA_GEN_TIMEOUT=N` — seconds per spec generation (default 300).

**Long runs (matrix / spec-gen / playwright) — don't wait blindly**: agent harnesses may background the command and lose the notification. Everything is durable: `matrix.json` is rewritten after every stage (`stage` field), live spec progress goes to `reports/<run>-matrix/playwright.log`, generation failures leave `specs/*.FAILED` markers. Poll those, not stdout.

## Commands

| CLI | What it does |
|---|---|
| `web-qa-register-project <alias> --target-url <url> [--backend-url <url>]` | Register a project, scaffold `.web-qa/` |
| `web-qa-doctor [--alias <a>] [--json]` | **Preflight**: deps, chromium build, LLM CLI, registry; with `--alias` also project path/config, frontend/backend reachability, login (incl. every role), app map, scenarios, specs-runner setup. Exit 0 = healthy, 1 = hard failure. Run it FIRST when anything misbehaves |
| `web-qa-resolve-project [path] [--alias <a>] [--json]` | cwd/alias → project record |
| `web-qa-explore --alias <a> [--max-pages N] [--viewport <name>] [--interactive] [--no-mine]` | Crawl → app.context.md (non-default viewport → `app.context.<name>.md`, both feed spec-gen). Dedup: query params and numeric ids collapse, max 2 entity cards per route template. Everything below the `<!-- manual -->` marker in app.context.md survives re-crawls — hand-written notes go there. **Route mining (default on)**: routes declared in the app's source (Next/Nuxt/SvelteKit file routers, `path:`/`<Route path>` configs) seed the crawl and land in the Routes table with an Origin column — a BFS over `<a href>` alone misses client-side navigation. Deterministic, zero tokens. **`--interactive` (opt-in)**: additionally clicks non-link clickables and harvests pushState URL changes — for apps whose navigation exists only at runtime. Non-deterministic pass, hence the flag. Mutation safety is enforced at the network level (every non-GET request is aborted during the pass), with two known limits: WebSocket frames on already-open connections bypass the valve, and a GET with server-side side effects is let through |
| `web-qa-generate --alias <a> (--diff <ref> \| --task "...") [--out f.md] [--prefix X] [--force]` | Scenario md from git diff or task text via `claude -p`, strict TC format, validated for `## TC-…` headers. RBAC-aware: a diff touching permissions/roles fans out into allowed+denied TC pairs per role (`**Role:**` annotations) |
| `web-qa-run --alias <a> [--scenarios "<glob>"] [--role <r>] [--viewport <name>] [--update-baseline] [--visual-threshold N]` | Passive scenario run: goto + visible-text vs Expected (30% threshold) + axe + visual. Mutating TCs marked MANUAL |
| `web-qa-spec-gen --alias <a> [--all] [--tc <id>] [--force] [--workers N]` | Generate `.spec.ts` from TCs via `claude -p` (parallel, default 3 workers). App map embedded in the prompt; every spec validated with `playwright test --list`, 1 retry with the error fed back. Cache covers TC + prompt + context |
| `web-qa-run-specs --alias <a> [-- <playwright args>]` | `npx playwright test` over generated specs |
| `web-qa-matrix --alias <a> [--list] [--roles a,b] [--viewports d,m] [--workers N] [--include-adhoc] [--skip-passive\|--skip-specs]` | **Deploy gate**: inventory of ALL project tests (scenario TCs + specs) + full run + consolidated matrix with route coverage (global AND per-role) and flaky markers. `--viewports` runs the passive stage per viewport (× roles); a device-viewport also runs specs under mobile emulation (second playwright project) (history in `.web-qa/history.json`). Exit 0 = safe to deploy, 1 = fail/error present. `--list` = inventory + coverage only |
| `web-qa-maintain --alias <a> [--report <json>] [--apply] [--workers N]` | Self-heal with three-way classification: test fragility → corrected spec (`*.spec.ts.proposed`; `--apply` overwrites with `.bak` + rollback); transient env failure → spec untouched, rerun advised; genuine app bug → assertions kept, `test.fixme` + entry appended to `BUGS.md`. Healers patch test fragility, never real bugs |
| `web-qa-kill [--dry-run]` | Kill orphaned playwright runners + headless browsers (matches only `ms-playwright` binaries and `@playwright/test` CLIs — never a regular browser) |

**Per-project specs runner setup (once):**
```bash
cd <project>/.web-qa
# NOT `npm init -y`: the ".web-qa" dir name is an invalid npm package name.
# NOT a bare `npm install` without package.json: npm walks UP and pollutes the app's own deps.
[ -f package.json ] || printf '{"name":"web-qa-specs","private":true}\n' > package.json
npm install -D @playwright/test
npx playwright install chromium
grep -q node_modules .gitignore 2>/dev/null || echo 'node_modules/' >> .gitignore
cp <skill root>/playwright.config.template.ts playwright.config.ts   # adjust baseURL
```
Pin `@playwright/test` to an exact version: every version pins an exact browser build, and an unplanned upgrade means an unplanned browser download.

## Workflows (for the agent)

### Onboarding a project (once)
1. `web-qa-register-project <alias> --target-url ... --backend-url ...`; put credentials into `projects.json → auth`
2. Make sure the dev server responds
3. `web-qa-explore --alias <alias>` → review `app.context.md`, fill `auth_login_hint` and other config keys
4. Specs runner setup (block above)

### "Test X"
1. Resolve project (`web-qa-resolve-project`), read `app.context.md`
2. `web-qa-generate --task "X"` → review/edit scenarios
3. `web-qa-run` (passive) and/or `web-qa-spec-gen` + `web-qa-run-specs` (mutating)
4. Failures → `web-qa-maintain` → real bugs go to `BUGS.md`, spec bugs get healed

### Pre-deploy gate
1. Dev servers up → `web-qa-matrix --alias <a>` → exit 0 = deploy, 1 = investigate
2. In a deploy script: `web-qa-matrix --alias <a> && ./deploy.sh`
3. Convention: `_`-prefixed specs are ad-hoc debug — excluded from the gate; prod-hidden features via `gate_exclude`

## Screenshots and visual judgment (for the agent)

The pipeline itself never sends screenshots to a model — visual regression is an algorithmic
pixel diff. **Visual judgment is YOUR job as the orchestrating agent:**

- Every passive run saves a per-page screenshot into `reports/<run>/` (named `<TC>-<route>.png`);
  Playwright specs keep failure screenshots and traces under `.web-qa/test-results/`.
- **Read those images** before reporting a finding: a screenshot confirms or refutes a suspected
  bug far better than matched keywords. Screenshot → Read → judge → only then report.
- When the user asks "show me how page X looks" or "verify this visually" — take an ad-hoc
  screenshot: for public pages `uv run playwright screenshot <url> shot.png`; for authenticated
  pages run the relevant scenario (`web-qa-run --scenarios <file>`) and Read its artifacts.
- Visual regression failures come with both the current screenshot and the baseline in
  `baseline/` — Read both and say what actually changed, not just the diff percentage.

## Business rules

- **Never auto-fix found product bugs.** Report only; the user decides.
- **Exploration is never run on a schedule** — it's a full crawl; only explicit `web-qa-explore`.
- **Visual baseline lives in the project repo** (commit it) so regressions work in CI.
- **Reports are gitignored** — every run writes a fresh one.
- **a11y severity:** WCAG critical/serious = bug, the rest = note.
- **Golden path first.** Scenarios start with the happy path, then edge cases.
- **Mutating tests create their own data** (`test_data_prefix`), act on it, delete it in `try/finally`. Never mutate pre-existing data.
- **TC classification: structural evidence beats the declaration, the declaration beats absence.** `**Type:** passive|mutating` is required on every TC; runners never guess intent from prose keywords, so TCs work identically in any language. A declared `passive` is overridden to mutating when **Steps:** contain a mutating HTTP op (`POST /x` in Expected is context, not an action — never evidence). A TC without `**Type:**` is treated as mutating: it gets a real spec instead of a green passive check for steps that never executed. `web-qa-generate` warns about contradictions (`type_conflicts`) right after generation.

## Troubleshooting

| Symptom | Diagnosis / fix |
|---|---|
| Anything misbehaves | `web-qa-doctor --alias <a>` first — it catches every issue below |
| `alias not found` | Register via `web-qa-register-project`, check `projects.json` |
| `no credentials for '<alias>'` | Fill `auth` (and optionally `roles`) in `projects.json` |
| `web-qa-spec-gen` → «claude CLI not found» | Claude Code CLI must be on PATH (`which claude`) |
| Specs mass-fail on selectors | App map is stale → `web-qa-explore`, then `web-qa-spec-gen --force`, then `web-qa-maintain` |
| Spec generation silently missing a TC | Check `specs/*.FAILED` markers; raise `WEBQA_GEN_TIMEOUT` for complex TCs |
| `npm install` from `.web-qa` polluted the app's package.json | `.web-qa` had no own package.json — create it (see setup), reinstall inside, remove the stray dep from the app |
| Visual diffs after a legitimate UI change | `web-qa-run --update-baseline` |
| False visual diffs on dynamic content | `visual_masks` (point dynamics) or `visual_exclude` (data-driven pages) in config.json |
| Hung/killed run left browser processes | `web-qa-kill` (use `--dry-run` first to see what it found) |
| Playwright download stalls (CDN unreachable) | Pin `@playwright/test` to a version whose browser build is already in `~/.cache/ms-playwright/` |
