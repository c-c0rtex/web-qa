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
from spec_gen import call_claude, validate_spec, load_app_context, load_seed, postprocess_spec, seed_prompt_section

FIX_PROMPT = """You are fixing a FAILING Playwright TypeScript spec for an existing web app.
The app itself is considered correct — the spec has wrong selectors, timing or assertions.

APP MAP (auto-crawled; REAL routes, form fields, button labels and table headers — trust it over guesses):
{app_context}
{seed_section}

CURRENT SPEC ({spec_name}):
{spec_code}

ACTUAL FAILURE OUTPUT from `playwright test` (contains the real page state / selector mismatches):
{errors}

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

Rules for case (a):
- Output ONLY raw TypeScript code — no markdown fences, no commentary
- Keep the same test intent and assertions coverage; fix only what makes it fail
- Selector texts/labels MUST come from the APP MAP or from the failure output (e.g. the "received" strings), never invented
- NEVER use `waitForLoadState('networkidle')` (SPAs with polling never go idle) — replace it with
  web-first assertions (`await expect(locator).toBeVisible()`), `page.waitForURL(...)`, or
  `waitForLoadState('domcontentloaded')`
- NEVER "fix" a spec by rerouting a UI step through `page.request` — the UI path IS the test;
  fix the selector/timing instead. API calls stay setup/teardown/verification only
- If an element genuinely does not exist anymore, replace the step with the closest real equivalent and add a `// MAINT:` comment explaining the change

Output the corrected .spec.ts now:
"""


def run_playwright_json(webqa: Path, out_json: Path, workers: int | None,
                        viewport: str | None = None) -> None:
    env = dict(os.environ, PLAYWRIGHT_JSON_OUTPUT_NAME=str(out_json))
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
             proj: dict | None = None) -> tuple[str, str | None, str | None, str, str, str | None]:
    """Returns (spec_name, out_file_or_None, error_or_None, kind, detail, probe_warning).
    kind: 'fix' | 'transient' | 'app-bug'."""
    prompt = FIX_PROMPT.format(
        app_context=app_context,
        seed_section=seed_section,
        spec_name=spec_path.name,
        spec_code=spec_path.read_text(encoding="utf-8")[:12000],
        errors="\n\n---\n\n".join(errors[:4]),
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
    ap.add_argument("--workers", type=int, default=3, help="parallel claude calls")
    ap.add_argument("--pw-workers", type=int, help="playwright --workers for the test run")
    args = ap.parse_args()

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
        run_playwright_json(webqa, report_path, args.pw_workers or proj.get("workers"), viewport_env(proj))
    if not report_path.is_file():
        print(json.dumps({"error": f"no report at {report_path}"}), file=sys.stderr)
        return 2

    fails = failing_specs_from_report(json.loads(report_path.read_text()))
    if not fails:
        print(json.dumps({"healed": [], "errors": [], "note": "no failing specs in report"}))
        return 0

    app_context = load_app_context(proj_dir)
    seed_section = seed_prompt_section(load_seed(proj_dir))
    summary = {"healed": [], "transient": [], "app_bugs": [], "errors": [], "probe_warnings": [],
               "mode": "apply" if args.apply else "propose"}
    print(f"[maintain] {len(fails)} failing spec(s), {args.workers} workers", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {}
        for fname, errors in fails.items():
            spec_path = specs_dir / fname
            if not spec_path.is_file():
                summary["errors"].append({"spec": fname, "error": "spec file not found"})
                continue
            futures[pool.submit(heal_one, spec_path, errors, app_context, webqa, args.apply, seed_section, proj)] = fname
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

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
