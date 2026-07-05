<h1 align="center">web-qa</h1>

<p align="center">
  Autonomous web-app QA skill for Claude Code —<br>
  explore once, generate specs, run with zero tokens.
</p>

<p align="center">
  <img src="https://img.shields.io/github/actions/workflow/status/c-c0rtex/web-qa/ci.yml?style=flat-square&label=CI" alt="CI">
  <img src="https://img.shields.io/badge/version-0.3.2-blue?style=flat-square" alt="Version 0.3.2">
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License: MIT">
  <img src="https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/uv-managed-DE5FE9?style=flat-square" alt="uv">
  <img src="https://img.shields.io/badge/Playwright-1.55+-2EAD33?style=flat-square&logo=playwright&logoColor=white" alt="Playwright 1.55+">
  <img src="https://img.shields.io/badge/Claude%20Code-skill-D97757?style=flat-square" alt="Claude Code Skill">
</p>

Autonomous web-app QA skill for [Claude Code](https://claude.com/claude-code): Playwright E2E, visual regression, axe-core accessibility — with test scenarios auto-generated from a git diff or a plain-text task, specs grounded in a real crawled map of your app, and a self-healing loop for failing tests.

One skill, many projects: each project keeps its own `.web-qa/` state (scenarios, specs, baselines, reports) inside its repo; the skill itself stays global.

## Why

When an AI writes the code, you become the QA. Letting the same AI test its own work is a trap: a model that misread the requirement will happily write a test that passes on the misread — green check, bug intact.

web-qa splits the jobs: the model *generates* the checks, a deterministic oracle — pixel diff, exit code, `playwright test` — delivers the verdict. In between: a markdown test plan you review in minutes.

## How it works

LLM-driven browser testing is usually done by driving a browser through MCP step by step — powerful, but token-hungry. This skill takes a cheaper route:

1. **Explore** — a plain Playwright crawler logs in and maps your app once: routes, forms, real button labels, table headers, OpenAPI request schemas → `app.context.md` (cached in your repo). Routes declared in your source (Next/Nuxt/SvelteKit file routers, router configs) are mined statically and crawled even when no link points at them; an opt-in `--interactive` pass clicks through runtime-only navigation with all non-GET requests blocked at the network level.
2. **Generate** — test cases (markdown) are produced from a git diff or a task description, grounded in that map.
3. **Automate** — each test case becomes a self-contained `.spec.ts` via a single `claude -p` call. The prompt embeds the app map, so selectors come from *real* labels, not guesses. Every spec is validated with `playwright test --list` before it's accepted (one retry with the parse error fed back).
4. **Run** — plain `npx playwright test` + axe-core injection + pixel-diff visual regression. No LLM in the loop at run time: running your suite costs zero tokens.
5. **Maintain** — a failing spec plus its real error output goes back to the LLM, which returns a corrected spec. Proposed by default (`*.spec.ts.proposed`); `--apply` to overwrite with backup and automatic rollback if the fix doesn't parse.

Plus a **deploy gate**: `web-qa-matrix` inventories *all* tests in a project (scenario TCs + specs), runs everything, reports route coverage and flaky tests, and returns exit 0/1 — drop it in front of your deploy script.

```
web-qa-matrix --alias my-app && ./deploy.sh
```

## Requirements

- Linux/macOS, Python 3.11+, Node.js 18+, [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://claude.com/claude-code) CLI on PATH (`claude`) — used for generation/healing only
- A web app running locally (or reachable) with login credentials

## Install

```bash
git clone https://github.com/c-c0rtex/web-qa ~/.claude/skills/web-qa
cd ~/.claude/skills/web-qa
./install.sh          # uv sync + chromium + projects.json from example
```

Onboard a project:

```bash
bin/web-qa-register-project my-app --target-url http://127.0.0.1:3000 --backend-url http://127.0.0.1:8000
# put credentials into projects.json → "auth": {"email": ..., "password": ...}
bin/web-qa-explore --alias my-app          # crawl → <project>/.web-qa/app.context.md
```

Set up the per-project specs runner (once — see SKILL.md, "Per-project specs runner setup").

## Talking to your agent

web-qa is a skill: normally you don't type the CLI yourself — you ask your coding agent, and it drives the pipeline. Prompts that work well:

| You say | The agent does |
|---|---|
| *"Set up web-qa for this project"* | registers the project, asks for credentials, runs `doctor`, crawls the app map, sets up the specs runner |
| *"Is web-qa healthy here?"* | `web-qa-doctor --alias …` — one ✅/⚠️/❌ line per check; the agent explains the failing ones |
| *"Test that checkout works"* | generates scenarios for the flow, shows you the markdown test plan, runs it, reports with screenshots |
| *"Did my branch break anything?"* | `generate --diff main` → specs → matrix → verdict with the failing tests explained |
| *"Run the full regression before I deploy"* | `web-qa-matrix` → exit code, route coverage, flaky list |
| *"I changed the role permissions — check all roles"* | RBAC directive kicks in: allowed+denied TC pairs per role, matrix `--roles admin,editor,viewer` |
| *"Test the mobile version of the orders page"* | `explore --viewport mobile` (mobile app map) → mobile scenarios → `run --viewport mobile` with its own baselines |
| *"This test keeps failing, fix it"* | `maintain` — proposes a healed spec from the real error output; applies only if you say so |
| *"The redesign is intentional — update the baselines"* | `run --update-baseline` |
| *"Show me how /orders looks right now"* | takes a screenshot, reads it, describes what it sees |

The agent-facing contract (which command for which intent, business rules, config keys) lives in [SKILL.md](SKILL.md) — that's what your agent reads when the skill activates.

## Everyday use (CLI)

| Command | Purpose |
|---|---|
| `web-qa-doctor --alias my-app` | Preflight: deps, browser, servers, credentials, setup — run first |
| `web-qa-generate --alias my-app --diff main` | Test scenarios for whatever your branch changed |
| `web-qa-generate --alias my-app --task "date filter on /orders"` | Scenarios from a plain-text task |
| `web-qa-spec-gen --alias my-app --all` | Scenarios → Playwright specs (parallel, validated) |
| `web-qa-run --alias my-app` | Passive checks: navigation, content, a11y, visual |
| `web-qa-matrix --alias my-app` | Everything at once + deploy verdict (exit code) |
| `web-qa-maintain --alias my-app --apply` | Heal failing specs from their real errors |
| `web-qa-kill` | Clean up orphaned headless browsers after a killed run |

RBAC and responsive are matrix dimensions: `web-qa-matrix --roles admin,viewer --viewports desktop,mobile` runs the passive stage per combination (mobile = real device emulation: touch, UA, DPR — via Playwright device descriptors), reports per-role route coverage, and a device viewport also runs specs under mobile emulation. Per-viewport visual baselines are kept apart (`@mobile` suffix).

All long runs are durable: the matrix report is rewritten after every stage, Playwright progress streams to a log file, generation failures leave `*.FAILED` markers. If your agent (or you) kill a run, the partial report is already on disk.

## Configuration

Machine-local registry `projects.json` (aliases, URLs, credentials — never committed) + per-project `.web-qa/config.json` (stack description, auth flow hint for generation, visual masks/excludes, deploy-gate excludes, test-data prefix…). Both documented in [SKILL.md](SKILL.md), examples in [projects.example.json](projects.example.json) and [config.example.json](config.example.json).

Useful environment variables:

- `WEBQA_CLAUDE_MODEL=sonnet|opus` — model for internal `claude -p` calls
- `WEBQA_GEN_TIMEOUT=600` — seconds per spec generation (default 300)
- `WEBQA_WORKERS=4` — Playwright workers (template config)

## Token cost, honestly

- Exploration, running tests, the matrix, visual diff, a11y: **zero tokens** — plain Python + Playwright.
- Visual regression is an algorithmic pixel diff against a committed baseline — no vision model looks at your screenshots. Screenshots are saved as run artifacts for you (or your agent) to inspect.
- Spec generation: one `claude -p` call per test case (prompt ≈ app map + TC, roughly 10–20k tokens each), cached — regeneration only happens when the TC, the prompt, or the app map changes. Text-only: generation is grounded in the crawled app map, not in screenshots.
- Healing: one call per failing spec.

A 30-test-case project costs roughly one medium Claude conversation to fully automate, then runs for free.

## Roadmap

See [ROADMAP.md](ROADMAP.md) — next: cross-agent support (`WEBQA_LLM_CMD` for Codex/Gemini/Kimi CLIs) and flake-aware healing with ARIA snapshots. Items marked `help wanted` are good first contributions.

## Attribution

MIT-licensed. If you use this project or build on it, a link back to [github.com/c-c0rtex/web-qa](https://github.com/c-c0rtex/web-qa) is appreciated — it's the only thing asked for.

Generated tests, reports, and app maps produced by this tool are yours, no strings attached.

## Third-party

- [axe-core](https://github.com/dequelabs/axe-core) (`runners/axe.min.js`) — © Deque Systems, [MPL-2.0](https://www.mozilla.org/en-US/MPL/2.0/), vendored unmodified.
- [Playwright](https://playwright.dev) — Apache-2.0, installed as a dependency.

The four-stage pipeline architecture was inspired by [Playwright's test agents](https://playwright.dev) (Planner / Generator / Healer) and [TestDino](https://testdino.com)'s writing on AI-driven Playwright testing. No code or text was copied — the ideas were reimplemented from scratch.

## License

[MIT](LICENSE) © [c-c0rtex](https://github.com/c-c0rtex)
