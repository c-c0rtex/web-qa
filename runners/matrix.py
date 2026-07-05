"""Pre-deploy test matrix — inventory + full run of everything the project has.

Collects ALL existing tests for a project:
  1. scenarios/*.md test cases  → executed via run_scenarios.py (passive; mutating → manual)
  2. specs/*.spec.ts            → executed via `npx playwright test` (json reporter)
     Ad-hoc/debug specs prefixed with `_` are EXCLUDED unless --include-adhoc.

Produces a single consolidated matrix (one row per test) and exits non-zero if
anything failed — usable as a deploy gate:

  web-qa-matrix --alias my-app && ./deploy.sh

Output:
  <project>/.web-qa/reports/<RUN-ID>-matrix/
    matrix.md      — human-readable matrix
    matrix.json    — machine-readable (rows + stats), for CI
    playwright-results.json — raw playwright json (if specs stage ran)

Flags:
  --skip-passive / --skip-specs   run only one half
  --include-adhoc                 include `_*.spec.ts` debug specs
  --list                          inventory only, run nothing (exit 0)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from explore import load_project, run_fixture_cmd, viewport_entry, viewport_env
from run_scenarios import split_tcs, extract_paths, classify, tc_roles, DEFAULT_BACKEND_PREFIXES

SKILL = Path(__file__).resolve().parent.parent


def now_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# ---------- inventory ----------

def collect_scenario_tcs(webqa: Path, backend_prefixes: tuple[str, ...]) -> list[dict]:
    rows = []
    scenarios_dir = webqa / "scenarios"
    for md in sorted(scenarios_dir.glob("*.md")) if scenarios_dir.is_dir() else []:
        for tc in split_tcs(md.read_text(encoding="utf-8")):
            fronts, _ = extract_paths(tc["body"], backend_prefixes)
            kind, _ = classify(tc["body"])
            rows.append({
                "source": "scenario", "file": md.name, "id": tc["id"],
                "title": tc["title"], "kind": kind, "status": "not-run",
                "paths": fronts, "roles": tc_roles(tc["body"]),
            })
    return rows


def collect_specs(webqa: Path, include_adhoc: bool,
                  exclude_globs: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """Returns (rows, excluded_names). exclude_globs come from config `gate_exclude` —
    specs for features hidden on prod (feature flags, build-args) don't belong in the gate."""
    import fnmatch
    rows, excluded = [], []
    specs_dir = webqa / "specs"
    for f in sorted(specs_dir.glob("*.spec.ts")) if specs_dir.is_dir() else []:
        adhoc = f.name.startswith("_")
        if adhoc and not include_adhoc:
            continue
        if any(fnmatch.fnmatch(f.name, g) for g in exclude_globs or []):
            excluded.append(f.name)
            continue
        rows.append({
            "source": "spec", "file": f.name, "id": "", "title": f.stem,
            "kind": "adhoc" if adhoc else "spec", "status": "not-run",
        })
    return rows, excluded


def rows_for_role(rows: list[dict], role: str | None) -> list[dict]:
    """Role-annotated TCs (`**Role:** viewer`) only enter combos with a matching role —
    an admin-written Expected must not produce false fails under viewer. With no --roles
    they stay in (the runner marks them skip with a hint)."""
    if not role:
        return rows
    rl = role.lower()
    return [r for r in rows if not r.get("roles") or rl in r["roles"]]


# ---------- stages ----------

def run_passive_stage(alias: str, scenario_rows: list[dict], role: str | None = None,
                      viewport: str | None = None) -> str | None:
    """Run run_scenarios.py, fold statuses back into scenario_rows. Returns report path or None."""
    cmd = [str(SKILL / ".venv" / "bin" / "python"), str(SKILL / "runners" / "run_scenarios.py"),
           "--alias", alias, "--no-fixtures"]  # matrix seeds once for ALL combos
    if role:
        cmd += ["--role", role]
    if viewport:
        cmd += ["--viewport", viewport]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        for r in scenario_rows:
            r["status"] = "error"
            r["note"] = "passive runner crashed"
        print(f"[matrix] passive runner failed:\n{proc.stderr[-1500:]}", file=sys.stderr)
        return None
    results = json.loads((Path(data["report"]).parent / "results.json").read_text())["results"]
    by_key = {(r["scenario_file"], r["id"]): r for r in results}
    for row in scenario_rows:
        res = by_key.get((row["file"], row["id"]))
        if res:
            row["status"] = res["status"]
            row["note"] = " · ".join(res.get("notes", []))[:160]
    return data["report"]


def run_specs_stage(webqa: Path, spec_rows: list[dict], run_dir: Path, workers: int | None = None,
                    viewport: str | None = None, mobile_device: str | None = None) -> None:
    """Run playwright on exactly the inventoried spec files, fold statuses into spec_rows."""
    if not (webqa / "playwright.config.ts").is_file():
        for r in spec_rows:
            r["status"] = "error"
            r["note"] = "no playwright.config.ts (see SKILL.md setup)"
        return
    out_json = run_dir / "playwright-results.json"
    log_path = run_dir / "playwright.log"
    files = [f"specs/{r['file']}" for r in spec_rows]
    env = dict(os.environ, PLAYWRIGHT_JSON_OUTPUT_NAME=str(out_json))
    if viewport:
        env["WEBQA_VIEWPORT"] = viewport  # picked up by playwright.config.template.ts
    if mobile_device:
        env["WEBQA_MOBILE_DEVICE"] = mobile_device  # adds a `mobile` project in the template config
    # json → file via env; line-reporter → live progress in playwright.log (tail -f to watch)
    cmd = ["npx", "playwright", "test", "--reporter=line,json"]
    if workers:
        cmd.append(f"--workers={workers}")
    print(f"[matrix] playwright progress: tail -f {log_path}", file=sys.stderr)
    # Own process group: on timeout we kill the WHOLE tree (npx → node workers → chromium),
    # otherwise browsers orphan and keep running after the runner dies.
    with open(log_path, "w") as log:
        proc = subprocess.Popen([*cmd, *files], cwd=webqa, env=env,
                                stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        try:
            rc = proc.wait(timeout=3600)
            exit_note = f"exit {rc}"
        except subprocess.TimeoutExpired:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            exit_note = "stage timeout (3600s), playwright process group killed"
    if not out_json.is_file():
        tail = log_path.read_text()[-1500:] if log_path.is_file() else ""
        for r in spec_rows:
            r["status"] = "error"
            r["note"] = f"playwright produced no report ({exit_note})"
        print(f"[matrix] playwright failed ({exit_note}):\n{tail}", file=sys.stderr)
        return

    # Walk the suite tree; playwright test.status: expected|unexpected|skipped|flaky
    file_statuses: dict[str, list[str]] = {}

    def walk(suite: dict) -> None:
        for sub in suite.get("suites", []):
            walk(sub)
        for spec in suite.get("specs", []):
            fname = Path(spec.get("file", "")).name
            for t in spec.get("tests", []):
                file_statuses.setdefault(fname, []).append(t.get("status", "unexpected"))

    data = json.loads(out_json.read_text())
    for s in data.get("suites", []):
        walk(s)

    for row in spec_rows:
        statuses = file_statuses.get(row["file"])
        if not statuses:
            row["status"] = "error"
            row["note"] = "not found in playwright report"
        elif any(s == "unexpected" for s in statuses):
            row["status"] = "fail"
        elif all(s == "skipped" for s in statuses):
            row["status"] = "skip"
        elif any(s == "flaky" for s in statuses):
            row["status"] = "flaky"
        else:
            row["status"] = "pass"
        row["note"] = row.get("note", "") or f"{len(statuses or [])} test(s)"


# ---------- coverage ----------

def routes_from_context(webqa: Path) -> list[str]:
    """Frontend routes from the app.context.md Routes table."""
    ctx = webqa / "app.context.md"
    if not ctx.is_file():
        return []
    routes: list[str] = []
    for m in re.finditer(r"^\|\s*`(/[^`\s]*)`\s*\|", ctx.read_text(encoding="utf-8"), re.MULTILINE):
        # collapse to one template per route: /orders/235 and code-mined /orders/{orderId}
        # are the same coverage unit as /orders/{id}
        r = re.sub(r"\{[^}]+\}", "{id}", m.group(1).split("?")[0])
        r = re.sub(r"/\d+(?=/|$)", "/{id}", r)
        if r not in routes:
            routes.append(r)
    return routes


def compute_coverage(webqa: Path, scenario_rows: list[dict], spec_rows: list[dict]) -> dict:
    routes = routes_from_context(webqa)
    if not routes:
        return {"routes_total": 0, "covered": 0, "uncovered": [],
                "note": "no routes in app.context.md (run web-qa-explore)"}
    tc_paths = {p for r in scenario_rows for p in r.get("paths", [])}
    spec_texts = []
    for r in spec_rows:
        f = webqa / "specs" / r["file"]
        if f.is_file():
            spec_texts.append(f.read_text(encoding="utf-8"))
    uncovered = []
    for route in routes:
        base = route.rstrip("/") or "/"
        if base == "/":
            hit = "/" in tc_paths or any(
                re.search(r"goto\(\s*['\"`](?:https?://[^'\"`]*?)?/['\"`]", t) for t in spec_texts)
        else:
            def norm(s: str) -> str:
                return re.sub(r"/\d+(?=/|$)", "/{id}", s)
            # spec files use concrete ids, TC texts may use either — match both forms
            spec_pat = re.escape(base).replace(re.escape("{id}"), r"(?:\d+|\{[a-z_]*id\})") + r"['\"`/?\s]"
            hit = any(norm(p) == base or norm(p).startswith(base + "/") for p in tc_paths) or any(
                re.search(spec_pat, t) for t in spec_texts)
        if not hit:
            uncovered.append(route)
    return {"routes_total": len(routes), "covered": len(routes) - len(uncovered),
            "uncovered": uncovered}


def coverage_by_role(webqa: Path, scenario_rows: list[dict]) -> dict:
    """Per-role route coverage — "this route was never tested as viewer". Specs are
    excluded: they authenticate with their own hardcoded account, not a matrix role."""
    active = [r for r in scenario_rows if r.get("status") != "skip"]  # orphan skip rows
    role_names = sorted({r["role"] for r in active if r.get("role", "-") != "-"})
    out: dict = {}
    for role in role_names:
        rows_r = [r for r in active if r["role"] == role]
        cov = compute_coverage(webqa, rows_r, [])
        out[role] = {"covered": cov["covered"], "total": cov["routes_total"],
                     "uncovered": cov["uncovered"]}
    return out


# ---------- history / flaky ----------

HISTORY_KEEP = 20
FLAKY_WINDOW = 5


def row_key(r: dict) -> str:
    return f"{r['source']}:{r['file']}:{r['id']}:{r.get('role', '-')}:{r.get('viewport', '-')}"


def update_history(webqa: Path, run_id: str, rows: list[dict]) -> set[str]:
    """Append this run to .web-qa/history.json (last HISTORY_KEEP runs kept).
    Returns row keys that both passed and failed within the last FLAKY_WINDOW runs."""
    hist_path = webqa / "history.json"
    try:
        history = json.loads(hist_path.read_text()) if hist_path.is_file() else []
    except json.JSONDecodeError:
        history = []
    # migrate pre-viewport 4-part keys so flaky detection survives the upgrade
    for h in history:
        h["statuses"] = {(k + ":-" if k.count(":") == 3 else k): v
                         for k, v in h.get("statuses", {}).items()}
    history.append({"run_id": run_id, "statuses": {row_key(r): r["status"] for r in rows}})
    history = history[-HISTORY_KEEP:]
    hist_path.write_text(json.dumps(history, ensure_ascii=False))
    flaky: set[str] = set()
    recent = history[-FLAKY_WINDOW:]
    for r in rows:
        k = row_key(r)
        sts = [h["statuses"].get(k) for h in recent if k in h.get("statuses", {})]
        if "pass" in sts and any(s in ("fail", "error") for s in sts):
            flaky.add(k)
    return flaky


# ---------- report ----------

def render_junit_xml(alias: str, run_id: str, rows: list[dict]) -> str:
    """Matrix rows as JUnit XML — the lingua franca of CI test reporting. One testcase
    per row; fail/error → <failure>, manual/skip/not-run → <skipped>."""
    from xml.sax.saxutils import escape, quoteattr
    cases: list[str] = []
    failures = skipped = 0
    for r in rows:
        bits = [r.get("file") or "", r.get("id") or "", r.get("role") or "", r.get("viewport") or ""]
        name = quoteattr("::".join(b for b in bits if b))
        status, note = r.get("status", "not-run"), escape(r.get("note") or "")
        if status in ("fail", "error"):
            failures += 1
            cases.append(f"    <testcase name={name}><failure message=\"{status}\">{note}</failure></testcase>")
        elif status == "pass":
            cases.append(f"    <testcase name={name}/>")
        else:
            skipped += 1
            cases.append(f"    <testcase name={name}><skipped message=\"{status}\"/></testcase>")
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            f"<testsuites name=\"web-qa-matrix\" tests=\"{len(rows)}\" failures=\"{failures}\">\n"
            f"  <testsuite name={quoteattr(alias + ' ' + run_id)} tests=\"{len(rows)}\" "
            f"failures=\"{failures}\" skipped=\"{skipped}\">\n"
            + "\n".join(cases) + "\n  </testsuite>\n</testsuites>\n")


STATUS_EMOJI = {"pass": "✅", "fail": "❌", "error": "⚠️", "manual": "✋",
                "skip": "⏭", "flaky": "🔁", "not-run": "·"}
GATE_BLOCKING = ("fail", "error")


def render_matrix_md(alias: str, run_id: str, rows: list[dict], stats: dict, gate_ok: bool,
                     coverage: dict, flaky_keys: set[str],
                     role_coverage: dict | None = None) -> str:
    lines = [
        f"# Test Matrix — {alias}",
        f"\n_Run: {run_id} UTC_",
        f"\n**Deploy gate: {'✅ PASS' if gate_ok else '❌ BLOCKED'}**",
        f"\n- Total tests: {len(rows)}",
    ]
    for st, n in sorted(stats.items()):
        lines.append(f"- {STATUS_EMOJI.get(st, '?')} {st}: {n}")
    if flaky_keys:
        lines.append(f"- 🔁 flaky (unstable over last {FLAKY_WINDOW} runs): {len(flaky_keys)}")
    if coverage.get("routes_total"):
        lines.append(f"\n**Route coverage:** {coverage['covered']}/{coverage['routes_total']} routes have tests")
        if coverage["uncovered"]:
            lines.append("Uncovered: " + ", ".join(f"`{r}`" for r in coverage["uncovered"]))
    for role, rc in (role_coverage or {}).items():
        line = f"- role `{role}`: {rc['covered']}/{rc['total']}"
        if rc["uncovered"]:
            line += " — uncovered: " + ", ".join(f"`{r}`" for r in rc["uncovered"][:8])
        lines.append(line)
    lines.append("\n| # | Source | File | TC | Role | VP | Kind | Status | Note |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        note = (r.get("note", "") or "").replace("|", "\\|")[:120]
        emoji = STATUS_EMOJI.get(r["status"], "?")
        flaky_mark = " 🔁" if row_key(r) in flaky_keys else ""
        lines.append(f"| {i} | {r['source']} | {r['file']} | {r['id']} | {r.get('role', '-')} "
                     f"| {r.get('viewport', '-')} | {r['kind']} | {emoji} {r['status']}{flaky_mark} | {note} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    ap.add_argument("--skip-passive", action="store_true")
    ap.add_argument("--skip-specs", action="store_true")
    ap.add_argument("--include-adhoc", action="store_true", help="include _*.spec.ts debug specs")
    ap.add_argument("--list", action="store_true", help="print inventory and exit (no run)")
    ap.add_argument("--workers", type=int, help="playwright --workers (default: project config)")
    ap.add_argument("--roles", help="comma-separated role names from config `roles`; "
                                    "passive stage runs once per role (RBAC matrix)")
    ap.add_argument("--viewports", help="comma-separated viewport names from config `viewports`; "
                                        "passive stage runs once per viewport (responsive matrix); "
                                        "a device-entry also runs specs under mobile emulation")
    ap.add_argument("--junit", help="also write the matrix as JUnit XML to this path (CI systems)")
    ap.add_argument("--no-fixtures", action="store_true",
                    help="skip the project's fixture_cmd for this run")

    args = ap.parse_args()

    proj = load_project(args.alias)
    webqa = Path(proj["path"]) / ".web-qa"
    backend_prefixes = tuple(proj.get("backend_prefixes") or DEFAULT_BACKEND_PREFIXES)

    roles = [r.strip() for r in args.roles.split(",") if r.strip()] if args.roles else [None]
    viewports = [v.strip() for v in args.viewports.split(",") if v.strip()] if args.viewports else [None]
    combo_sets: list[tuple[str | None, str | None, list[dict]]] = []
    scenario_rows: list[dict] = []
    for role in roles:
        for vp in viewports:
            rws = rows_for_role(collect_scenario_tcs(webqa, backend_prefixes), role)
            for r in rws:
                r["role"] = role or "-"
                r["viewport"] = vp or "-"
            combo_sets.append((role, vp, rws))
            scenario_rows.extend(rws)
    # a requested device-viewport (e.g. iPhone) also turns on the mobile project for specs
    mobile_device = next(
        (d for v in viewports if v for d in [viewport_entry(proj, v).get("device")] if d),
        None,
    )
    spec_rows, gate_excluded = collect_specs(webqa, args.include_adhoc,
                                             proj.get("gate_exclude") or [])
    for r in spec_rows:
        r["role"] = "-"
    # role-annotated TCs whose declared roles are outside --roles must not vanish silently —
    # surface them as skip rows so the inventory stays complete
    requested = {(r or "").lower() for r in roles if r}
    if requested:
        for r in collect_scenario_tcs(webqa, backend_prefixes):
            if r.get("roles") and not (set(r["roles"]) & requested):
                r.update(role=",".join(r["roles"]), viewport="-", status="skip",
                         note="declared roles not included in --roles")
                scenario_rows.append(r)
    rows = scenario_rows + spec_rows
    if gate_excluded:
        print(f"[matrix] gate_exclude skipped {len(gate_excluded)} spec(s): "
              f"{', '.join(gate_excluded[:6])}{'…' if len(gate_excluded) > 6 else ''}", file=sys.stderr)
    if not rows:
        print(json.dumps({"error": f"no tests found in {webqa}"}), file=sys.stderr)
        return 2

    coverage = compute_coverage(webqa, scenario_rows, spec_rows)
    role_coverage = coverage_by_role(webqa, scenario_rows)

    if args.list:
        print(json.dumps({"total": len(rows), "scenario_tcs": len(scenario_rows),
                          "specs": len(spec_rows), "gate_excluded": gate_excluded,
                          "coverage": coverage, "role_coverage": role_coverage,
                          "rows": rows}, ensure_ascii=False, indent=2))
        return 0

    # Seed deterministic data ONCE per matrix run (not per role/viewport combo — combos
    # must see the same world). Passive runners inside the stages skip their own seeding.
    if not args.no_fixtures:
        run_fixture_cmd(proj)

    run_id = now_run_id()
    run_dir = webqa / "reports" / f"{run_id}-matrix"
    run_dir.mkdir(parents=True, exist_ok=True)
    matrix_md = run_dir / "matrix.md"
    passive_reports: list[str] = []

    def snapshot(stage: str, flaky_keys: set[str] = frozenset()) -> tuple[dict, bool]:
        """Persist matrix.md/json NOW. Called before and after every stage so a killed
        or hung run still leaves a durable partial report on disk."""
        stats: dict[str, int] = {}
        for r in rows:
            stats[r["status"]] = stats.get(r["status"], 0) + 1
        gate_ok = not any(r["status"] in GATE_BLOCKING for r in rows)
        matrix_md.write_text(render_matrix_md(proj["alias"], run_id, rows, stats, gate_ok,
                                              coverage, flaky_keys, role_coverage))
        (run_dir / "matrix.json").write_text(json.dumps({
            "run_id": run_id, "alias": proj["alias"], "stage": stage, "gate_ok": gate_ok,
            "stats": stats, "coverage": coverage, "role_coverage": role_coverage,
            "flaky": sorted(flaky_keys),
            "gate_excluded": gate_excluded,
            "roles": [r or "default" for r in roles],
            "viewports": [v or "default" for v in viewports],
            "passive_reports": passive_reports, "rows": rows,
        }, ensure_ascii=False, indent=2))
        return stats, gate_ok

    snapshot("inventory")  # durable from second zero, statuses filled in as stages finish
    if scenario_rows and not args.skip_passive:
        for role, vp, rws in combo_sets:
            label = "".join([f" (role {role})" if role else "", f" (viewport {vp})" if vp else ""])
            print(f"[matrix] passive stage{label}: {len(rws)} TC", file=sys.stderr)
            rep = run_passive_stage(args.alias, rws, role, vp)
            if rep:
                passive_reports.append(rep)
            snapshot(f"passive{label}")
    if spec_rows and not args.skip_specs:
        print(f"[matrix] specs stage: {len(spec_rows)} spec files", file=sys.stderr)
        # CLI --workers > config `workers` (small dev stands want 1: parallel chromiums
        # against one dev server turn timing into noise) > template default
        run_specs_stage(webqa, spec_rows, run_dir, args.workers or proj.get("workers"),
                        viewport_env(proj), mobile_device)
        snapshot("specs")

    flaky_keys = update_history(webqa, run_id, rows)
    stats, gate_ok = snapshot("final", flaky_keys)

    if args.junit:
        junit_path = Path(args.junit)
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        junit_path.write_text(render_junit_xml(proj["alias"], run_id, rows))
        print(f"[matrix] junit → {junit_path}", file=sys.stderr)

    # Zero-config CI summary: inside GitHub Actions the matrix lands on the run page
    gss = os.environ.get("GITHUB_STEP_SUMMARY")
    if gss:
        with open(gss, "a", encoding="utf-8") as f:
            f.write("\n" + matrix_md.read_text(encoding="utf-8"))

    if not args.no_fixtures:
        run_fixture_cmd(proj, teardown=True)

    print(json.dumps({"run_id": run_id, "gate_ok": gate_ok, "stats": stats,
                      "total": len(rows), "matrix": str(matrix_md)}, ensure_ascii=False))
    return 0 if gate_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
