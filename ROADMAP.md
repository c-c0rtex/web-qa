# Roadmap

Directional, not contractual — ordered by value/effort. Suggestions and PRs welcome,
especially for items marked `help wanted`.

_Last updated: 2026-07_

## Now (v0.2)

- [ ] **Preserve manual edits in `app.context.md`** — re-running `web-qa-explore` currently
  overwrites the whole file, including hand-written business-logic notes. Everything below a
  `<!-- manual -->` marker must survive re-crawls. *(known flaw, top priority)*
- [ ] **`web-qa-doctor`** — one-command preflight: registry/config valid, target/backend reachable,
  credentials work, chromium build present in cache, `claude` CLI on PATH. Every support issue so
  far would have been caught by it.
- [ ] **ARIA snapshots in maintain** — recent Playwright writes `error-context.md` with an ARIA
  snapshot of the page at failure time; feeding it to the healing prompt should sharply improve
  fix accuracy over raw error text.
- [ ] **storageState caching** — login once per project, reuse the session across runs with
  staleness detection, instead of logging in on every run.

## Next (v0.3)

- [ ] **Route coverage × roles** — the matrix already knows routes and roles; cross them to show
  "this route was never tested as `viewer`".
- [ ] **JUnit XML / GitHub Actions summary from the matrix** — first-class CI consumption:
  annotations on PRs, `$GITHUB_STEP_SUMMARY` table, standard test-report formats. `help wanted`
- [ ] **Opt-in vision for maintain (`--with-screens`)** — attach the failure screenshot to the
  healing prompt. Deliberately opt-in: images cost ~1.5k tokens each; the pipeline stays
  vision-free by default.
- [ ] **Seeded fixture data** — a per-project seed hook so visual baselines and mutating tests run
  against a stable dataset instead of live data. Makes `visual_exclude` unnecessary on most pages.
- [ ] **Flaky quarantine** — tests flagged flaky by history stop blocking the deploy gate
  (reported separately) until they stabilize.
- [ ] **Config schema** — JSON Schema for `config.json`/`projects.json` with validation in
  `web-qa-doctor` and editor autocompletion. `help wanted`

## Later

- [ ] **Pluggable LLM backend** — `claude -p` is the only generation backend today; abstract it to
  a configurable command so codex/gemini CLIs work too. `help wanted`
- [ ] **Pluggable reporters** — webhook/Slack/Telegram notifications for run results (extracted
  from the private predecessor of this repo; returns once its messaging skill is open-sourced).
- [ ] **Multi-browser projects** — firefox/webkit in the template config, matrix column per browser.
- [ ] **Responsive testing** — mobile viewports as first-class run variants, per-viewport baselines.
- [ ] **Single-file HTML report** — the matrix as a self-contained shareable page.
- [ ] **Claude Code plugin packaging** — install via the plugin marketplace instead of git clone.

## Shipped

- [x] v0.1 — 4-stage pipeline (explore → generate → automate → run), self-healing maintain,
  deploy-gate matrix with route coverage and flaky detection, RBAC roles, visual regression with
  masks/excludes, axe-core a11y, uv packaging, CI, 16 unit tests.
