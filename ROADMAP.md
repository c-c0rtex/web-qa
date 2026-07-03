# Roadmap

web-qa is an autonomous web-app QA skill for AI coding agents. This is a living
document describing current direction — not a set of promises. Issues and
feedback are welcome and will shape what moves up.

_Last updated: 2026-07_

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
  races) apart from real regressions — retry the former, heal the latter. Plus ARIA snapshots
  (`error-context.md`) in the healing prompt instead of raw error text

## Exploring

Directional. Shape may change; not committed.

- Richer assertions beyond DOM / pixel / a11y — network assertions, console-error gating,
  performance budgets
- API testing from OpenAPI — a deterministic contract layer (schemathesis integration,
  zero tokens) + LLM-generated API flow scenarios (`web-qa-generate --api`), endpoint
  coverage in the matrix
- Parallel spec execution for larger apps
- Machine- and human-readable reports — JUnit XML / `$GITHUB_STEP_SUMMARY` for CI,
  single-file HTML with diff artifacts inline `help wanted`
- A review workflow for auto-generated baselines
- Seeded fixture data — stable dataset for baselines and mutating tests
- Opt-in screenshot in the healing prompt (`--with-screens`) — healing already spends tokens;
  the run phase stays vision-free

## Out of scope (for now)

- Being a general browser-automation agent — web-qa is a QA runner, not a driver
- Sending screenshots to a vision model **during the run phase** — runs stay zero-token and
  deterministic by design
