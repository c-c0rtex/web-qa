# Roadmap

web-qa is an autonomous web-app QA skill for AI coding agents. This is a living
document describing current direction — not a set of promises. Issues and
feedback are welcome and will shape what moves up.

_Last updated: 2026-07_

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

### v0.2 — hardening

- [ ] **Manual-edit-safe app map** — re-running `web-qa-explore` overwrites hand-written notes
  in `app.context.md`; everything below a `<!-- manual -->` marker must survive re-crawls
  *(known flaw)*
- [ ] **`web-qa-doctor`** — one-command preflight: config valid, target/backend reachable,
  credentials work, chromium build cached, LLM CLI on PATH. Every onboarding issue so far
  would have been caught by it
- [ ] **Configurable viewport** — per-project `viewport` in config, applied consistently across
  explore, the passive runner and the specs config (today: hardcoded, and inconsistent
  between layers)

### v0.3 — cross-agent & smarter healing

- [ ] **Cross-agent support** — decouple generation from the `claude -p` binary behind a
  configurable command (`WEBQA_LLM_CMD`), so the same skill runs under Codex CLI, Gemini CLI,
  Kimi Code CLI. The deterministic run phase is already agent-agnostic. Ships with a
  compatibility matrix and installs under the shared `.agents/skills/` path
- [ ] **Healing v2** — flake-aware: tell transient failures (network timeouts, rate limits,
  races) apart from real regressions — retry the former, heal the latter. Plus ARIA snapshots
  (`error-context.md`) in the healing prompt instead of raw error text

## Exploring

Directional. Shape may change; not committed.

- Richer assertions beyond DOM / pixel / a11y — network assertions, console-error gating,
  performance budgets
- API testing from OpenAPI — a deterministic contract layer (schemathesis integration,
  zero tokens) + LLM-generated API flow scenarios (`web-qa-generate --api`), endpoint
  coverage in the matrix
- Responsive testing — viewports as a matrix dimension (Playwright device descriptors),
  per-viewport baselines, mobile-aware exploration
- Parallel spec execution for larger apps
- Machine- and human-readable reports — JUnit XML / `$GITHUB_STEP_SUMMARY` for CI,
  single-file HTML with diff artifacts inline `help wanted`
- A review workflow for auto-generated baselines
- Seeded fixture data — stable dataset for baselines and mutating tests
- Route coverage × roles — "this route was never tested as `viewer`"
- Opt-in screenshot in the healing prompt (`--with-screens`) — healing already spends tokens;
  the run phase stays vision-free

## Out of scope (for now)

- Being a general browser-automation agent — web-qa is a QA runner, not a driver
- Sending screenshots to a vision model **during the run phase** — runs stay zero-token and
  deterministic by design
