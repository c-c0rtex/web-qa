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
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from explore import load_project, viewport_env
from spec_gen import call_claude, validate_spec, load_app_context, postprocess_spec

FIX_PROMPT = """You are fixing a FAILING Playwright TypeScript spec for an existing web app.
The app itself is considered correct — the spec has wrong selectors, timing or assertions.

APP MAP (auto-crawled; REAL routes, form fields, button labels and table headers — trust it over guesses):
{app_context}

CURRENT SPEC ({spec_name}):
{spec_code}

ACTUAL FAILURE OUTPUT from `playwright test` (contains the real page state / selector mismatches):
{errors}

TASK: Output the FULL corrected .spec.ts file. Rules:
- Output ONLY raw TypeScript code — no markdown fences, no commentary
- Keep the same test intent and assertions coverage; fix only what makes it fail
- Selector texts/labels MUST come from the APP MAP or from the failure output (e.g. the "received" strings), never invented
- NEVER use `waitForLoadState('networkidle')` (SPAs with polling never go idle) — replace it with
  web-first assertions (`await expect(locator).toBeVisible()`), `page.waitForURL(...)`, or
  `waitForLoadState('domcontentloaded')`
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


def heal_one(spec_path: Path, errors: list[str], app_context: str, webqa: Path,
             apply_fix: bool) -> tuple[str, str | None, str | None]:
    """Returns (spec_name, out_file_or_None, error_or_None)."""
    prompt = FIX_PROMPT.format(
        app_context=app_context,
        spec_name=spec_path.name,
        spec_code=spec_path.read_text(encoding="utf-8")[:12000],
        errors="\n\n---\n\n".join(errors[:4]),
    )
    try:
        code = postprocess_spec(call_claude(prompt))
    except Exception as e:
        return spec_path.name, None, f"claude failed: {e}"
    if not code.strip():
        return spec_path.name, None, "empty output from claude"

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
            return spec_path.name, None, f"fix did not parse, rolled back: {parse_err[:300]}"
    return spec_path.name, str(out_file), None


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
        run_playwright_json(webqa, report_path, args.pw_workers, viewport_env(proj))
    if not report_path.is_file():
        print(json.dumps({"error": f"no report at {report_path}"}), file=sys.stderr)
        return 2

    fails = failing_specs_from_report(json.loads(report_path.read_text()))
    if not fails:
        print(json.dumps({"healed": [], "errors": [], "note": "no failing specs in report"}))
        return 0

    app_context = load_app_context(proj_dir)
    summary = {"healed": [], "errors": [], "mode": "apply" if args.apply else "propose"}
    print(f"[maintain] {len(fails)} failing spec(s), {args.workers} workers", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {}
        for fname, errors in fails.items():
            spec_path = specs_dir / fname
            if not spec_path.is_file():
                summary["errors"].append({"spec": fname, "error": "spec file not found"})
                continue
            futures[pool.submit(heal_one, spec_path, errors, app_context, webqa, args.apply)] = fname
        for fut in as_completed(futures):
            name, out_file, err = fut.result()
            if err:
                summary["errors"].append({"spec": name, "error": err})
                print(f"[maintain] FAIL {name}: {err[:200]}", file=sys.stderr)
            else:
                summary["healed"].append({"spec": name, "out": out_file})
                print(f"[maintain] OK   {name} → {out_file}", file=sys.stderr)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
