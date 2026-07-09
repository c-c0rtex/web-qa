"""Phase 5 — Maintenance: heal failing specs using their real error context.

Loop: run (or reuse) a playwright json report → for every failing spec feed
claude the spec source + actual error (selector text, timeout, assertion diff)
→ get a corrected spec → validate with `playwright test --list`.

Per the skill's business rule maintenance PROPOSES, it does not silently apply:
by default the fix is written next to the spec as `<name>.spec.ts.proposed`;
`--apply` overwrites the spec in place (previous version saved as `.bak`).

Usage:
  maintain.py --alias my-app                 # run tests, propose fixes
  maintain.py --alias my-app --report <playwright-results.json>  # reuse a report
  maintain.py --alias my-app --apply         # write fixes in place (+ .bak)
  maintain.py --alias my-app --workers 3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from explore import load_project, viewport_env
from spec_gen import (DEFAULT_MAX_USD, call_claude, llm_spend, load_app_context, load_seed,
                      postprocess_spec, seed_prompt_section, validate_spec)

FIX_PROMPT = """You are fixing a FAILING Playwright TypeScript spec for an existing web app.
The app itself is considered correct — the spec has wrong selectors, timing or assertions.

APP MAP (auto-crawled; REAL routes, form fields, button labels and table headers — trust it over guesses):
{app_context}
{seed_section}

CURRENT SPEC ({spec_name}):
{spec_code}

ACTUAL FAILURE OUTPUT from `playwright test` (contains the real page state / selector mismatches):
{errors}
{failure_context_section}
TASK — FIRST classify the failure, THEN act:
(a) TEST FRAGILITY (selector drift, timing, wrong assumption about the page) → output the
    corrected spec
(b) TRANSIENT ENVIRONMENT FAILURE (connection refused, dev server down, 5xx from
    infrastructure, network timeout unrelated to the app logic) → output the ORIGINAL spec
    UNCHANGED with one added first line: `// TRANSIENT: <reason>` — do not "fix" flakiness
    into the code
(c) GENUINE APPLICATION BUG (the app really violates the test's Expected) → keep the
    assertions EXACTLY as they are (never weaken them to make the test pass), replace
    `test(` with `test.fixme(` and add a first line `// APP-BUG: <one-line description>` —
    healers patch test fragility, not real bugs

WHEN NOT TO CHOOSE (c) — a numeric or textual mismatch is NOT evidence of an app bug:
- If the spec RE-IMPLEMENTS the value it checks (recomputes a KPI, a total, a count from raw
  collections), the mismatch is a tie between two implementations and the spec's is the
  unverified one. Unless the APP MAP or the spec's own comments state the metric's definition
  AND that definition matches what the spec computes, this is case (a): the oracle is wrong.
  Watch for the classics — counting rows of the wrong entity, summing the wrong currency,
  comparing a wire enum against the label the UI renders for it, treating a `—` placeholder
  as `''`
- A control that is PRESENT but `[disabled]` is not a permission leak. Read the page state in
  the failure context before calling `toHaveCount(0)` a genuine RBAC bug
- Choose (c) only when the failure needs no re-derivation to be wrong: a 500, a crash, a
  drill-down that contradicts the summary it drills into, an invariant the app itself breaks

Rules for case (a):
- Output ONLY raw TypeScript code — no markdown fences, no commentary
- Keep the same test intent and assertions coverage; fix only what makes it fail
- Selector texts/labels MUST come from the APP MAP or from the failure output (e.g. the "received" strings), never invented
- The PAGE STATE AT FAILURE below (when present) is the real DOM: take roles and accessible
  names from it. An element it shows with no accessible name cannot be reached by
  `getByLabel` / `getByRole(name)` at all — locate it structurally
- NEVER use `waitForLoadState('networkidle')` (SPAs with polling never go idle) — replace it with
  web-first assertions (`await expect(locator).toBeVisible()`), `page.waitForURL(...)`, or
  `waitForLoadState('domcontentloaded')`
- NEVER "fix" a spec by rerouting a UI step through `page.request` — the UI path IS the test;
  fix the selector/timing instead. API calls stay setup/teardown/verification only
- If an element genuinely does not exist anymore, replace the step with the closest real equivalent and add a `// MAINT:` comment explaining the change

Output the corrected .spec.ts now:
"""


def run_playwright_json(webqa: Path, out_json: Path, workers: int | None,
                        viewport: str | None = None, artifacts_dir: Path | None = None) -> None:
    env = dict(os.environ, PLAYWRIGHT_JSON_OUTPUT_NAME=str(out_json))
    if artifacts_dir:
        env["WEBQA_OUTPUT_DIR"] = str(artifacts_dir)
    if viewport:
        env["WEBQA_VIEWPORT"] = viewport
    cmd = ["npx", "playwright", "test", "--reporter=json"]
    if workers:
        cmd.append(f"--workers={workers}")
    # Own process group + group-kill on timeout, so node/chromium don't orphan
    proc = subprocess.Popen(cmd, cwd=webqa, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def failing_specs_from_report(report: dict) -> dict[str, list[str]]:
    """file name → list of error strings."""
    fails: dict[str, list[str]] = {}

    def walk(suite: dict) -> None:
        for sub in suite.get("suites", []):
            walk(sub)
        for spec in suite.get("specs", []):
            fname = Path(spec.get("file", "")).name
            for t in spec.get("tests", []):
                if t.get("status") != "unexpected":
                    continue
                for res in t.get("results", []):
                    for err in ([res.get("error")] if res.get("error") else res.get("errors", [])):
                        msg = (err or {}).get("message", "")
                        if msg:
                            fails.setdefault(fname, []).append(msg[:2500])

    for s in report.get("suites", []):
        walk(s)
    return fails


ERROR_CONTEXT_CHARS = 14000


def artifact_prefix(spec_name: str) -> str:
    """`catalogs__tc-ref5-crud.spec.ts` → `catalogs__tc-ref5-crud`.

    Playwright names its result dir `<file-without-.spec.ts>-<title>-<project>`. The old
    code used `Path(name).stem`, which strips only `.ts` and leaves `.spec` glued on, so the
    prefix never matched a single directory and the page snapshot below was never once read.
    A healer that thinks it has the failing page's DOM, and silently doesn't, is worse than
    one that knows it doesn't."""
    return re.sub(r"\.spec\.ts$", "", spec_name)[:24].lower()


def failure_artifacts(webqa: Path, spec_name: str, artifacts_dir: Path | None = None) -> dict:
    """error-context.md content + failure screenshot paths for a spec, harvested from
    playwright's test-results/. The error-context file carries an ARIA snapshot of the
    page AT THE MOMENT of failure — far stronger healing input than the error text alone.
    Playwright truncates result-dir names, so match on a raw name prefix.

    `artifacts_dir` points at an archived copy (`reports/<run-id>/test-results`); playwright
    wipes the live `test-results/` at the start of every run, so healing from an older
    report needs the archive."""
    out: dict = {"error_context": None, "screens": []}
    tr = artifacts_dir or (webqa / "test-results")
    if not tr.is_dir():
        return out
    prefix = artifact_prefix(spec_name)
    for d in sorted(tr.iterdir()):
        if not d.is_dir() or not d.name.lower().startswith(prefix):
            continue
        ec = d / "error-context.md"
        if ec.is_file() and out["error_context"] is None:
            out["error_context"] = ec.read_text(encoding="utf-8", errors="ignore")[:ERROR_CONTEXT_CHARS]
        out["screens"] += [str(p) for p in sorted(d.glob("*.png"))]
    return out


def artifacts_for_report(webqa: Path, report_path: Path) -> Path | None:
    """The test-results/ that belongs to THIS report, not to whatever ran last.

    `matrix` writes `reports/<run-id>/matrix/playwright-results.json` next to
    `reports/<run-id>/test-results/`. Healing an old report against the live directory means
    feeding the healer another run's page snapshots — confidently, and wrongly."""
    for cand in (report_path.parent / "test-results",
                 report_path.parent.parent / "test-results"):
        if cand.is_dir():
            return cand
    legacy = webqa / "test-results"
    return legacy if legacy.is_dir() else None


def failure_context_section(error_context: str | None) -> str:
    if not error_context:
        return ""
    return ("\nPAGE STATE AT FAILURE (playwright error-context — what the page actually "
            "looked like when it failed; trust this over assumptions):\n"
            f"{error_context}\n")


RE_TRANSIENT = re.compile(r"^\s*//\s*TRANSIENT:\s*(.+)$", re.MULTILINE)
RE_APP_BUG = re.compile(r"^\s*//\s*APP-BUG:\s*(.+)$", re.MULTILINE)


def classify_heal_output(code: str) -> tuple[str, str]:
    """('transient'|'app-bug'|'fix', detail) from the healer's leading marker comment.
    Only the first lines count — a marker buried mid-code is not a classification."""
    head = "\n".join(code.splitlines()[:3])
    m = RE_TRANSIENT.search(head)
    if m:
        return "transient", m.group(1).strip()
    m = RE_APP_BUG.search(head)
    if m:
        return "app-bug", m.group(1).strip()
    return "fix", ""


# Deterministic transient signatures: network / infra / rate-limit / gateway strings that
# mean "the environment hiccuped", NOT "the app is broken". Curated to be SAFE — a real
# assertion or missing-element failure never contains these, so matching one can't mask a
# genuine regression. Generic locator timeouts are deliberately absent (they usually ARE
# real: the element is gone).
TRANSIENT_SIGNATURES = [
    r"net::ERR_[A-Z_]+", r"ERR_CONNECTION[A-Z_]*", r"ERR_NETWORK[A-Z_]*",
    r"ECONNRESET", r"ECONNREFUSED", r"ETIMEDOUT", r"EAI_AGAIN", r"ENOTFOUND",
    r"socket hang up", r"getaddrinfo", r"read ECONN",
    r"\b429\b", r"Too Many Requests", r"rate.?limit",
    r"\b50[234]\b", r"Bad Gateway", r"Service Unavailable", r"Gateway Time-?out",
    r"Target (?:page, context or browser has been )?closed",
    r"Execution context was destroyed", r"frame (?:was )?detached",
    r"Protocol error \(", r"WebSocket .*closed",
]
RE_TRANSIENT_SIG = re.compile("|".join(TRANSIENT_SIGNATURES), re.IGNORECASE)


def transient_signature(errors: list[str]) -> str | None:
    """The matched network/infra/rate-limit signature in the failure output, or None.
    Deterministic and cheap — runs BEFORE the LLM so an env hiccup is retried, not healed."""
    m = RE_TRANSIENT_SIG.search("\n".join(errors))
    return m.group(0) if m else None


def rerun_is_flaky(webqa: Path, spec_name: str, times: int, workers: int | None,
                   viewport: str | None) -> bool:
    """Re-run ONE failing spec `times` times; True if it passed at least once — i.e. the
    failure is non-deterministic (flake), so it should be retried, not healed. False if it
    failed every rerun (a consistent, real failure). Reproducibility as the oracle, no LLM."""
    if times < 1:
        return False
    out_json = webqa / "reports" / f"rerun-{Path(spec_name).stem}.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PLAYWRIGHT_JSON_OUTPUT_NAME=str(out_json))
    # Its own output dir, or this rerun deletes the very failure artifacts the healer is
    # about to read: playwright clears outputDir on start, and the reruns run BEFORE heal_one.
    env["WEBQA_OUTPUT_DIR"] = str(webqa / "reports" / "rerun-test-results")
    if viewport:
        env["WEBQA_VIEWPORT"] = viewport
    cmd = ["npx", "playwright", "test", f"specs/{spec_name}", "--reporter=json",
           f"--repeat-each={times}", f"--workers={workers or 1}"]
    proc = subprocess.Popen(cmd, cwd=webqa, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        proc.wait(timeout=900)
    except subprocess.TimeoutExpired:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return False
    if not out_json.is_file():
        return False
    try:
        report = json.loads(out_json.read_text())
    except json.JSONDecodeError:
        return False
    # passed at least once across the repeats → non-deterministic → flake
    return _spec_passed_any(report)


def _spec_passed_any(report: dict) -> bool:
    seen_pass = False

    def walk(suite: dict) -> None:
        nonlocal seen_pass
        for sub in suite.get("suites", []):
            walk(sub)
        for spec in suite.get("specs", []):
            for t in spec.get("tests", []):
                for res in t.get("results", []):
                    if res.get("status") in ("passed", "expected"):
                        seen_pass = True

    for s in report.get("suites", []):
        walk(s)
    return seen_pass


def record_app_bug(proj_dir: Path, spec_name: str, description: str) -> None:
    """Append a healer-confirmed application bug to .web-qa/BUGS.md."""
    bugs = proj_dir / ".web-qa" / "BUGS.md"
    if not bugs.is_file():
        bugs.write_text("# BUGS\n\n")
    entry = f"[maintain] `{spec_name}`: {description}"
    if entry in bugs.read_text(encoding="utf-8"):
        return  # same bug already recorded on a previous run
    with bugs.open("a", encoding="utf-8") as f:
        f.write(f"- {date.today().isoformat()} {entry}\n")


def heal_one(spec_path: Path, errors: list[str], app_context: str, webqa: Path,
             apply_fix: bool, seed_section: str = "",
             proj: dict | None = None,
             artifacts_dir: Path | None = None) -> tuple[str, str | None, str | None, str, str, str | None]:
    """Returns (spec_name, out_file_or_None, error_or_None, kind, detail, probe_warning).
    kind: 'fix' | 'transient' | 'app-bug'."""
    artifacts = failure_artifacts(webqa, spec_path.name, artifacts_dir)
    prompt = FIX_PROMPT.format(
        app_context=app_context,
        seed_section=seed_section,
        spec_name=spec_path.name,
        spec_code=spec_path.read_text(encoding="utf-8")[:12000],
        errors="\n\n---\n\n".join(errors[:4]),
        failure_context_section=failure_context_section(artifacts["error_context"]),
    )
    try:
        code = postprocess_spec(call_claude(prompt))
    except Exception as e:
        return spec_path.name, None, f"claude failed: {e}", "fix", "", None
    if not code.strip():
        return spec_path.name, None, "empty output from claude", "fix", "", None

    kind, detail = classify_heal_output(code)
    if kind == "transient":
        # nothing to write — the spec is fine, the environment hiccuped
        return spec_path.name, None, None, kind, detail, None

    if apply_fix:
        spec_path.with_suffix(spec_path.suffix + ".bak").write_text(
            spec_path.read_text(encoding="utf-8"), encoding="utf-8")
        out_file = spec_path
    else:
        out_file = spec_path.with_suffix(spec_path.suffix + ".proposed")
    out_file.write_text(code, encoding="utf-8")

    # Validate parseability; .proposed can't be --list'ed (wrong extension), check applied only
    if apply_fix:
        parse_err = validate_spec(webqa, out_file)
        if parse_err:
            # roll back
            out_file.write_text(out_file.with_suffix(out_file.suffix + ".bak").read_text(encoding="utf-8"),
                                encoding="utf-8")
            return spec_path.name, None, f"fix did not parse, rolled back: {parse_err[:300]}", kind, detail, None
    probe_warning = None
    if kind == "fix" and proj is not None:
        # report-only: a healer that "fixed" the spec into non-existent elements should be
        # visible immediately, not on the next failing run
        from locator_probe import probe_feedback, probe_spec
        probe_warning = probe_feedback(probe_spec(out_file, proj))
    return spec_path.name, str(out_file), None, kind, detail, probe_warning


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    ap.add_argument("--report", help="existing playwright-results.json; default: run tests now")
    ap.add_argument("--apply", action="store_true", help="overwrite specs in place (keeps .bak)")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel claude calls (default 1). Concurrent calls all miss the "
                         "shared prompt cache — each extra worker buys wall-clock with money")
    ap.add_argument("--max-usd", type=float, default=None,
                    help=f"hard LLM spend ceiling for this run (default {DEFAULT_MAX_USD:.2f}, "
                         f"0 = no guard). Also settable via WEBQA_MAX_USD")
    ap.add_argument("--pw-workers", type=int, help="playwright --workers for the test run")
    ap.add_argument("--with-screens", action="store_true",
                    help="list failure screenshot paths in the summary JSON so the "
                         "orchestrating agent can Read them (visual judgment is its job)")
    ap.add_argument("--reruns", type=int, default=0,
                    help="before healing, re-run a signature-clean failing spec N times; "
                         "if it ever passes it's a flake (retry, don't heal). Default 0 (off)")
    ap.add_argument("--artifacts-dir",
                    help="archived test-results/ to read failure page snapshots from "
                         "(reports/<run-id>/test-results). Default: the live test-results/, "
                         "which playwright wipes at the start of every run")
    args = ap.parse_args()
    if args.max_usd is not None:
        os.environ["WEBQA_MAX_USD"] = str(args.max_usd)

    proj = load_project(args.alias)
    proj_dir = Path(proj["path"])
    webqa = proj_dir / ".web-qa"
    specs_dir = webqa / "specs"

    if args.report:
        report_path = Path(args.report)
    else:
        report_path = webqa / "reports" / "maintain-playwright-results.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        print("[maintain] running playwright to collect failures…", file=sys.stderr)
        run_playwright_json(webqa, report_path, args.pw_workers or proj.get("workers"),
                            viewport_env(proj), webqa / "reports" / "test-results")
    if not report_path.is_file():
        print(json.dumps({"error": f"no report at {report_path}"}), file=sys.stderr)
        return 2

    fails = failing_specs_from_report(json.loads(report_path.read_text()))
    if not fails:
        print(json.dumps({"healed": [], "errors": [], "note": "no failing specs in report"}))
        return 0

    if args.artifacts_dir:
        artifacts_dir = Path(args.artifacts_dir)
        if not artifacts_dir.is_dir():
            print(json.dumps({"error": f"no artifacts dir at {artifacts_dir}"}), file=sys.stderr)
            return 2
    else:
        artifacts_dir = artifacts_for_report(webqa, report_path)
    if artifacts_dir:
        print(f"[maintain] failure artifacts: {artifacts_dir}", file=sys.stderr)
    else:
        print("[maintain] no failure artifacts found — healing from error text alone",
              file=sys.stderr)
    app_context = load_app_context(proj_dir)
    seed_section = seed_prompt_section(load_seed(proj_dir))
    summary = {"healed": [], "transient": [], "app_bugs": [], "errors": [], "probe_warnings": [],
               "mode": "apply" if args.apply else "propose"}
    print(f"[maintain] {len(fails)} failing spec(s), {args.workers} workers", file=sys.stderr)

    # Deterministic-first: classify flake/transient WITHOUT the LLM before spending a heal.
    #  1. a network/infra/rate-limit signature in the failure = env hiccup → transient
    #  2. --reruns N: a signature-clean spec that passes on re-run = non-deterministic → transient
    # Only specs that survive both reach the healer.
    to_heal: dict[str, list[str]] = {}
    for fname, errors in fails.items():
        sig = transient_signature(errors)
        if sig:
            summary["transient"].append({"spec": fname, "reason": sig, "source": "signature"})
            print(f"[maintain] TRANSIENT {fname}: signature '{sig}' (spec untouched — rerun)", file=sys.stderr)
            continue
        if args.reruns and (specs_dir / fname).is_file() and rerun_is_flaky(
                webqa, fname, args.reruns, args.pw_workers or proj.get("workers"), viewport_env(proj)):
            summary["transient"].append({"spec": fname, "reason": f"passed on re-run (×{args.reruns})",
                                         "source": "rerun"})
            print(f"[maintain] TRANSIENT {fname}: passed on re-run — flake, not healed", file=sys.stderr)
            continue
        to_heal[fname] = errors

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {}
        for fname, errors in to_heal.items():
            spec_path = specs_dir / fname
            if not spec_path.is_file():
                summary["errors"].append({"spec": fname, "error": "spec file not found"})
                continue
            futures[pool.submit(heal_one, spec_path, errors, app_context, webqa, args.apply,
                                seed_section, proj, artifacts_dir)] = fname
        for fut in as_completed(futures):
            name, out_file, err, kind, detail, probe_warning = fut.result()
            if probe_warning:
                summary["probe_warnings"].append({"spec": name, "warning": probe_warning})
                print(f"[maintain] PROBE {name}: healed spec has unresolved locators\n{probe_warning}",
                      file=sys.stderr)
            if err:
                summary["errors"].append({"spec": name, "error": err})
                print(f"[maintain] FAIL {name}: {err[:200]}", file=sys.stderr)
            elif kind == "transient":
                summary["transient"].append({"spec": name, "reason": detail})
                print(f"[maintain] TRANSIENT {name}: {detail[:120]} (spec untouched — rerun)", file=sys.stderr)
            elif kind == "app-bug":
                summary["app_bugs"].append({"spec": name, "bug": detail, "out": out_file})
                record_app_bug(proj_dir, name, detail)
                print(f"[maintain] APP-BUG {name}: {detail[:120]} → test.fixme + BUGS.md", file=sys.stderr)
            else:
                summary["healed"].append({"spec": name, "out": out_file})
                print(f"[maintain] OK   {name} → {out_file}", file=sys.stderr)

    if args.with_screens:
        summary["screens"] = {fname: failure_artifacts(webqa, fname, artifacts_dir)["screens"]
                              for fname in fails}

    summary["llm"] = llm_spend()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
