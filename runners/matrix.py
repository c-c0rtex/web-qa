"""Pre-deploy test matrix — inventory + full run of everything the project has.

Collects ALL existing tests for a project:
  1. scenarios/*.md test cases  → executed via run_scenarios.py (passive; mutating → manual)
  2. specs/*.spec.ts            → executed via `npx playwright test` (json reporter)
     Ad-hoc/debug specs prefixed with `_` are EXCLUDED unless --include-adhoc.

Produces a single consolidated matrix (one row per test) and exits non-zero if
anything failed — usable as a deploy gate:

  web-qa-matrix --alias my-app && ./deploy.sh

Output:
  <project>/.web-qa/reports/<RUN-ID>/          — passive runner report
  <project>/.web-qa/reports/<RUN-ID>/matrix/   — matrix.md/.json + specs artifacts
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
import threading
from datetime import datetime, timezone
from pathlib import Path

from coverage import control_coverage
from progress import Progress, emit
from explore import (export_spec_env, load_project, redacted_env, run_fixture_cmd,
                     viewport_entry, viewport_env)
from spec_gen import orphan_specs
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


RE_SPEC_TC = re.compile(r"__(tc-[a-z]*\d+)", re.IGNORECASE)
# Write verbs against the backend. The login POST is the one write every spec performs and
# it mutates nothing, so it must not make the whole suite look mutating.
RE_SPEC_WRITE = re.compile(r"\.(?:post|put|patch|delete)\s*\(\s*[`'\"][^`'\"]*", re.IGNORECASE)


def spec_tc_id(filename: str) -> str:
    """`catalogs__tc-ref5-crud.spec.ts` → `TC-REF5`; '' for ad-hoc/hand-written specs."""
    m = RE_SPEC_TC.search(filename)
    return m.group(1).upper() if m else ""


def spec_is_mutating(text: str, tc_kind: str | None) -> bool:
    """Does running this spec write to the app's data?

    The TC's declared kind is authoritative when we can find it. Specs with no matching TC
    (hand-written, ad-hoc) are read from source: any non-auth write verb counts.

    This exists because the mutation gate used to live only in the passive runner. `matrix`
    handed every .spec.ts to playwright regardless, so a run that reported 78 mutating test
    cases as "✋ manual" had already created, edited and deleted rows through the specs."""
    if tc_kind == "mutating":
        return True
    if tc_kind == "passive":
        return False
    for m in RE_SPEC_WRITE.finditer(text):
        if "/auth/" not in m.group(0) and "/login" not in m.group(0):
            return True
    return False


def collect_specs(webqa: Path, include_adhoc: bool,
                  exclude_globs: list[str] | None = None,
                  tc_kinds: dict[str, str] | None = None,
                  orphans: set[str] | None = None) -> tuple[list[dict], list[str]]:
    """Returns (rows, excluded_names). exclude_globs come from config `gate_exclude` —
    specs for features hidden on prod (feature flags, build-args) don't belong in the gate.

    `tc_kinds` maps TC id → passive|mutating so each spec row can declare whether it writes.
    Without that the Kind column said `spec` for all of them and mutations were invisible."""
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
        tc_id = spec_tc_id(f.name)
        mutating = spec_is_mutating(f.read_text(encoding="utf-8", errors="ignore"),
                                    (tc_kinds or {}).get(tc_id))
        if f.name in (orphans or set()):
            # No test case defines it any more (a TC was deleted, or its title reworded and
            # spec-gen wrote a new file). Running it would let a stale, unexplained spec mutate
            # data and block the deploy gate.
            rows.append({
                "source": "spec", "file": f.name, "id": tc_id, "title": f.stem,
                "kind": "orphan", "mutating": mutating, "status": "manual",
                "skipped_mutating": True,
                "note": "orphan: no test case defines this spec (web-qa-spec-gen --prune)",
            })
            continue
        rows.append({
            "source": "spec", "file": f.name, "id": tc_id, "title": f.stem,
            "kind": ("adhoc" if adhoc else "spec") + (" (mutating)" if mutating else ""),
            "mutating": mutating, "status": "not-run",
        })
    return rows, excluded


DEFAULT_KEEP_ARTIFACTS = 3


def artifact_stats(artifacts_dir: Path) -> dict:
    """Files and bytes playwright left behind for this run. Empty dict when nothing failed."""
    if not artifacts_dir.is_dir():
        return {}
    files = [f for f in artifacts_dir.rglob("*") if f.is_file()]
    if not files:
        return {}
    return {"dir": str(artifacts_dir), "files": len(files),
            "bytes": sum(f.stat().st_size for f in files)}


def prune_artifacts(webqa: Path, keep: int) -> list[str]:
    """Delete `test-results/` from all but the `keep` newest runs; the reports themselves stay.

    Traces are megabytes apiece. Keeping every run's forever turns .web-qa into gigabytes, but
    keeping none is what let run N+1 erase the evidence of run N. Run ids sort lexically."""
    import shutil
    reports = webqa / "reports"
    if keep < 0 or not reports.is_dir():
        return []
    have = sorted((d for d in reports.iterdir() if (d / "test-results").is_dir()),
                  key=lambda d: d.name)
    pruned = []
    for d in have[:max(0, len(have) - keep)]:
        shutil.rmtree(d / "test-results", ignore_errors=True)
        pruned.append(d.name)
    return pruned


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
                      viewport: str | None = None, run_dir: Path | None = None) -> str | None:
    """Run run_scenarios.py, fold statuses back into scenario_rows. Returns report path or None."""
    cmd = [str(SKILL / ".venv" / "bin" / "python"), str(SKILL / "runners" / "run_scenarios.py"),
           "--alias", alias, "--no-fixtures"]  # matrix seeds once for ALL combos
    if role:
        cmd += ["--role", role]
    if viewport:
        cmd += ["--viewport", viewport]
    if run_dir is not None:
        # the single default combo owns the run folder; role/viewport combos get a subfolder
        combo = "-".join(filter(None, [role, viewport]))
        cmd += ["--reports-dir", str(run_dir / combo if combo else run_dir)]
    # stdout is captured (its last line is the summary JSON); stderr is INHERITED so the
    # child's own `[run] 12/59 · …` progress reaches the terminal live. Capturing both meant
    # a multi-minute stage per role printed nothing at all until it was over.
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, timeout=3600)
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        for r in scenario_rows:
            r["status"] = "error"
            r["note"] = "passive runner crashed"
        emit("matrix", f"passive runner failed (exit {proc.returncode}); its stderr is above")
        return None
    results = json.loads((Path(data["report"]).parent / "results.json").read_text())["results"]
    by_key = {(r["scenario_file"], r["id"]): r for r in results}
    for row in scenario_rows:
        res = by_key.get((row["file"], row["id"]))
        if res:
            row["status"] = res["status"]
            row["note"] = " · ".join(res.get("notes", []))[:160]
    return data["report"]


# The `line` reporter rewrites one terminal line, so every progress line arrives prefixed
# with cursor-control escapes (`ESC[1A ESC[2K`). Matching at `^` therefore matched nothing —
# the specs stage printed its header and then went silent for the whole run.
RE_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RE_PW_LINE = re.compile(r"\[(\d+)/(\d+)\]\s+\[[^\]]+\]\s+›\s+(\S+?):\d+:\d+\s+›\s*(.*)$")
RE_PW_FAIL = re.compile(r"^\s*\d+\)\s+\[[^\]]+\]\s+›\s+(\S+?):\d+:\d+\s+›\s*(.*)$")


def _pump_playwright(stream, log, bar: "Progress") -> None:
    """Everything playwright says goes to the log; only progress and failures reach the
    terminal. Raw playwright output is a wall of stack traces — the point of a progress line
    is that a human can read it while the run is going."""
    for line in stream:
        log.write(line)
        log.flush()
        clean = RE_ANSI.sub("", line).rstrip()
        m = RE_PW_LINE.search(clean)
        if m:
            i, _n, spec, title = m.groups()
            bar.done = int(i) - 1                    # playwright owns the counter
            bar.step(f"{Path(spec).name}  {title[:70]}", "RUN")
            continue
        m = RE_PW_FAIL.match(clean)
        if m:
            bar.note(f"FAIL {Path(m.group(1)).name}  {m.group(2)[:70]}")


def run_specs_stage(webqa: Path, spec_rows: list[dict], run_dir: Path, workers: int | None = None,
                    viewport: str | None = None, mobile_device: str | None = None,
                    artifacts_dir: Path | None = None) -> None:
    """Run playwright on exactly the inventoried spec files, fold statuses into spec_rows."""
    if not (webqa / "playwright.config.ts").is_file():
        for r in spec_rows:
            r["status"] = "error"
            r["note"] = "no playwright.config.ts (see SKILL.md setup)"
        return
    to_run = [r for r in spec_rows if not r.get("skipped_mutating")]
    if not to_run:
        return
    out_json = run_dir / "playwright-results.json"
    log_path = run_dir / "playwright.log"
    files = [f"specs/{r['file']}" for r in to_run]
    env = dict(os.environ, PLAYWRIGHT_JSON_OUTPUT_NAME=str(out_json))
    if artifacts_dir:
        # playwright clears its outputDir on start; pointing it at THIS run's folder is what
        # keeps the previous run's screenshots and page snapshots alive
        env["WEBQA_OUTPUT_DIR"] = str(artifacts_dir)
    if viewport:
        env["WEBQA_VIEWPORT"] = viewport  # picked up by playwright.config.template.ts
    if mobile_device:
        env["WEBQA_MOBILE_DEVICE"] = mobile_device  # adds a `mobile` project in the template config
    # json → file via env; line-reporter → progress. The log used to be the ONLY place that
    # progress appeared, so a 40-minute specs stage looked like a hung process.
    cmd = ["npx", "playwright", "test", "--reporter=line,json"]
    if workers:
        cmd.append(f"--workers={workers}")
    bar = Progress("specs", len(to_run))
    bar.start(f"{len(to_run)} spec file(s) via playwright — full log: {log_path}")
    # Own process group: on timeout we kill the WHOLE tree (npx → node workers → chromium),
    # otherwise browsers orphan and keep running after the runner dies.
    with open(log_path, "w") as log:
        proc = subprocess.Popen([*cmd, *files], cwd=webqa, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, start_new_session=True)
        pump = threading.Thread(target=_pump_playwright, args=(proc.stdout, log, bar), daemon=True)
        pump.start()
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
        pump.join(timeout=10)
    bar.finish(exit_note)
    if not out_json.is_file():
        tail = log_path.read_text()[-1500:] if log_path.is_file() else ""
        for r in to_run:
            r["status"] = "error"
            r["note"] = f"playwright produced no report ({exit_note})"
        emit("matrix", f"playwright failed ({exit_note}):\n{tail}")
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

    for row in to_run:
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


def _flipped(window: list[dict], rows: list[dict]) -> set[str]:
    """Row keys that BOTH passed and failed within the given history window — the
    definition of unstable, shared by flaky reporting and quarantine."""
    out: set[str] = set()
    for r in rows:
        k = row_key(r)
        sts = [h["statuses"].get(k) for h in window if k in h.get("statuses", {})]
        if "pass" in sts and any(s in ("fail", "error") for s in sts):
            out.add(k)
    return out


def update_history(webqa: Path, run_id: str, rows: list[dict],
                   quarantine_window: int = 0) -> tuple[set[str], set[str]]:
    """Append this run to .web-qa/history.json (last HISTORY_KEEP runs kept). Returns
    (flaky_keys, quarantined_keys): flaky = flipped over the last FLAKY_WINDOW runs
    (reported with 🔁); quarantined = flipped over the last `quarantine_window` runs
    (0 = off) — those are kept out of the gate's exit code until they stabilize."""
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
    flaky = _flipped(history[-FLAKY_WINDOW:], rows)
    quarantined = _flipped(history[-quarantine_window:], rows) if quarantine_window else set()
    return flaky, quarantined


# ---------- report ----------

def fold_mutating_into_specs(scenario_rows: list[dict],
                             spec_rows: list[dict]) -> tuple[list[dict], int]:
    """One executed test, one row.

    The passive runner cannot mutate; a mutating test case is EXECUTED by its spec. Emitting a
    scenario row for it too — once per role, none of them running anything — put the same test
    case in the matrix four times and reported the three copies as `✋ manual`. Work that was
    done looked undone.

    So a mutating TC that has a spec keeps no scenario row at all: the spec row already carries
    its id, its kind and its real verdict. A mutating TC with NO spec keeps exactly one row,
    honestly `manual`, because nothing executed it. Passive TCs are untouched — when they also
    have a spec, the two rows are two different checks (a11y/visual/heuristic vs. asserted)."""
    has_spec = {r["id"] for r in spec_rows if r["id"]}
    kept: list[dict] = []
    seen_specless: set[str] = set()
    folded = 0
    for row in scenario_rows:
        if row.get("kind") != "mutating":
            kept.append(row)
            continue
        if row["id"] in has_spec:
            folded += 1
            continue
        if row["id"] in seen_specless:      # same TC across role combos: one row is enough
            folded += 1
            continue
        seen_specless.add(row["id"])
        row["role"] = "-"
        row["note"] = "mutating, and no spec exists: nothing executed it (web-qa-spec-gen)"
        kept.append(row)
    return kept, folded


def render_junit_xml(alias: str, run_id: str, rows: list[dict],
                     quarantined: set[str] = frozenset()) -> str:
    """Matrix rows as JUnit XML — the lingua franca of CI test reporting. One testcase
    per row; fail/error → <failure>, manual/skip/not-run → <skipped>. A quarantined
    failure is emitted as <skipped> so CI aggregators don't block on it — same rule as
    the gate exit code."""
    from xml.sax.saxutils import escape, quoteattr
    cases: list[str] = []
    failures = skipped = 0
    for r in rows:
        bits = [r.get("file") or "", r.get("id") or "", r.get("role") or "", r.get("viewport") or ""]
        name = quoteattr("::".join(b for b in bits if b and b != "-"))
        status, note = r.get("status", "not-run"), escape(r.get("note") or "")
        if status in ("fail", "error") and quarantined and row_key(r) in quarantined:
            skipped += 1
            cases.append(f"    <testcase name={name}><skipped message=\"quarantined ({status})\"/></testcase>")
        elif status in ("fail", "error"):
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
                     role_coverage: dict | None = None,
                     quarantined: set[str] = frozenset(),
                     control_gaps: dict[str, list[str]] | None = None) -> str:
    control_gaps = control_gaps or {}
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
    if quarantined:
        q_blocking = sum(1 for r in rows
                         if r["status"] in GATE_BLOCKING and row_key(r) in quarantined)
        lines.append(f"- 🚧 quarantined (excluded from the gate until stable): "
                     f"{len(quarantined)}" + (f", masking {q_blocking} failing" if q_blocking else ""))
    if coverage.get("routes_total"):
        lines.append(f"\n**Route coverage:** {coverage['covered']}/{coverage['routes_total']} routes have tests")
        if coverage["uncovered"]:
            lines.append("Uncovered: " + ", ".join(f"`{r}`" for r in coverage["uncovered"]))
    if control_gaps:
        total = sum(len(n) for n in control_gaps.values())
        lines.append(f"\n**Untouched controls:** {total} on {len(control_gaps)} route(s) "
                     f"— no test case names them")
        for route, names in sorted(control_gaps.items(), key=lambda kv: -len(kv[1]))[:5]:
            shown = ", ".join(f"«{n}»" for n in names[:6])
            more = f" +{len(names) - 6}" if len(names) > 6 else ""
            lines.append(f"- `{route}` ({len(names)}): {shown}{more}")
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
        mark = (" 🔁" if row_key(r) in flaky_keys else "") + (" 🚧" if row_key(r) in quarantined else "")
        lines.append(f"| {i} | {r['source']} | {r['file']} | {r['id']} | {r.get('role', '-')} "
                     f"| {r.get('viewport', '-')} | {r['kind']} | {emoji} {r['status']}{mark} | {note} |")
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
    ap.add_argument("--no-mutations", action="store_true",
                    help="do not write to the app's data: mutating specs are reported "
                         "`manual` instead of run. The passive stage never mutates anyway, "
                         "so without this flag a 'passive' matrix run still mutates via specs")
    ap.add_argument("--keep-artifacts", type=int, default=None,
                    help=f"how many runs' test-results/ folders to keep on disk "
                         f"(default {DEFAULT_KEEP_ARTIFACTS}, config `keep_artifacts`; "
                         f"-1 = keep everything). Traces are megabytes per failure")

    args = ap.parse_args()

    proj = load_project(args.alias)
    export_spec_env(proj)      # stand URLs and logins for the specs — never in their source
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
    tc_kinds = {r["id"]: r["kind"] for r in collect_scenario_tcs(webqa, backend_prefixes)}
    orphans = set(orphan_specs(webqa))
    if orphans:
        emit("matrix", f"{len(orphans)} orphan spec(s) reported and NOT run (no test case defines them): {', '.join(sorted(orphans)[:5])}{'…' if len(orphans) > 5 else ''}")
    spec_rows, gate_excluded = collect_specs(webqa, args.include_adhoc,
                                             proj.get("gate_exclude") or [], tc_kinds, orphans)
    for r in spec_rows:
        r["role"] = "-"
        if args.no_mutations and r.get("mutating"):
            r["skipped_mutating"] = True
            r["status"] = "manual"
            r["note"] = "skipped (mutating spec); --no-mutations"
    mutating_specs = sum(1 for r in spec_rows if r.get("mutating"))
    if mutating_specs and not args.no_mutations and not args.skip_specs:
        emit("matrix", f"{mutating_specs} of {len(spec_rows)} spec(s) WRITE to the app's data (pass --no-mutations for a read-only run)")
    # role-annotated TCs whose declared roles are outside --roles must not vanish silently —
    # surface them as skip rows so the inventory stays complete
    requested = {(r or "").lower() for r in roles if r}
    if requested:
        for r in collect_scenario_tcs(webqa, backend_prefixes):
            if r.get("roles") and not (set(r["roles"]) & requested):
                r.update(role=",".join(r["roles"]), viewport="-", status="skip",
                         note="declared roles not included in --roles")
                scenario_rows.append(r)
    # Coverage is computed from the full scenario list below; the matrix DISPLAYS one row per
    # executed test, so a mutating TC lives in its spec's row, not in a duplicate of its own.
    displayed_scenarios, folded = fold_mutating_into_specs(scenario_rows, spec_rows) \
        if not args.skip_specs else (scenario_rows, 0)
    if folded:
        emit("matrix", f"{folded} mutating scenario row(s) folded into the specs that execute them")
    rows = displayed_scenarios + spec_rows
    if gate_excluded:
        emit("matrix", f"gate_exclude skipped {len(gate_excluded)} spec(s): {', '.join(gate_excluded[:6])}{'…' if len(gate_excluded) > 6 else ''}")
    if not rows:
        print(json.dumps({"error": f"no tests found in {webqa}"}), file=sys.stderr)
        return 2

    coverage = compute_coverage(webqa, scenario_rows, spec_rows)
    role_coverage = coverage_by_role(webqa, scenario_rows)
    # A route with one test case that only reads a table is not a tested route.
    control_gaps = control_coverage(webqa)

    if args.list:
        print(json.dumps({"total": len(rows), "scenario_tcs": len(scenario_rows),
                          "specs": len(spec_rows), "gate_excluded": gate_excluded,
                          "coverage": coverage, "role_coverage": role_coverage,
                          "uncovered_controls": control_gaps,
                          "rows": rows}, ensure_ascii=False, indent=2))
        return 0

    # Seed deterministic data ONCE per matrix run (not per role/viewport combo — combos
    # must see the same world). Passive runners inside the stages skip their own seeding.
    if not args.no_fixtures:
        run_fixture_cmd(proj)

    # Teardown must survive a Ctrl-C and a crash. Two killed runs left QA- entities behind,
    # and the next run's `POST /shipments` came back 409 Conflict on a duplicate it had
    # created itself — read, at first, as the application being broken.
    try:
        run_id = now_run_id()
        # one run, one folder: matrix artifacts at the top, each passive combo nested below
        run_dir = webqa / "reports" / run_id
        matrix_dir = run_dir / "matrix"
        matrix_dir.mkdir(parents=True, exist_ok=True)
        matrix_md = matrix_dir / "matrix.md"
        artifacts_dir = run_dir / "test-results"
        passive_reports: list[str] = []
        # What produced this report. Absent it, nobody — including the next reader of the
        # matrix — can tell whether the run skipped the passive stage, ran two workers, or
        # mutated the database. The stats alone cannot answer any of that.
        invocation = {
            "argv": sys.argv,
            "cwd": os.getcwd(),
            "flags": {k: v for k, v in sorted(vars(args).items()) if v not in (None, False)},
            "env": redacted_env(),      # passwords from export_spec_env must not land in a report
        }
        artifacts: dict = {}

        def snapshot(stage: str, flaky_keys: set[str] = frozenset(),
                     quarantined: set[str] = frozenset()) -> tuple[dict, bool]:
            """Persist matrix.md/json NOW. Called before and after every stage so a killed
            or hung run still leaves a durable partial report on disk. Quarantined rows still
            run and report, but their fail/error does not block the gate."""
            stats: dict[str, int] = {}
            for r in rows:
                stats[r["status"]] = stats.get(r["status"], 0) + 1
            gate_ok = not any(r["status"] in GATE_BLOCKING and row_key(r) not in quarantined
                              for r in rows)
            matrix_md.write_text(render_matrix_md(proj["alias"], run_id, rows, stats, gate_ok,
                                                  coverage, flaky_keys, role_coverage, quarantined,
                                                  control_gaps))
            (matrix_dir / "matrix.json").write_text(json.dumps({
                "run_id": run_id, "alias": proj["alias"], "stage": stage, "gate_ok": gate_ok,
                "invocation": invocation, "artifacts": artifacts,
                "mutating_specs_run": 0 if args.no_mutations or args.skip_specs else mutating_specs,
                "orphan_specs": sorted(orphans),
                "stats": stats, "coverage": coverage, "role_coverage": role_coverage,
                "uncovered_controls": control_gaps,
                "flaky": sorted(flaky_keys), "quarantined": sorted(quarantined),
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
                emit("matrix", f"passive stage{label}: {len(rws)} TC")
                rep = run_passive_stage(args.alias, rws, role, vp, run_dir=run_dir)
                if rep:
                    passive_reports.append(rep)
                snapshot(f"passive{label}")
        if spec_rows and not args.skip_specs:
            runnable = sum(1 for r in spec_rows if not r.get("skipped_mutating"))
            emit("matrix", f"specs stage: {runnable} of {len(spec_rows)} spec files")
            # CLI --workers > config `workers` (small dev stands want 1: parallel chromiums
            # against one dev server turn timing into noise) > template default
            run_specs_stage(webqa, spec_rows, matrix_dir, args.workers or proj.get("workers"),
                            viewport_env(proj), mobile_device, artifacts_dir)
            artifacts.update(artifact_stats(artifacts_dir))
            if artifacts:
                emit("matrix", f"failure artifacts → {artifacts_dir} ({artifacts['files']} files, {artifacts['bytes'] // 1024} KB)")
            snapshot("specs")

        keep = args.keep_artifacts if args.keep_artifacts is not None else \
            int(proj.get("keep_artifacts", DEFAULT_KEEP_ARTIFACTS))
        pruned = prune_artifacts(webqa, keep)
        if pruned:
            emit("matrix", f"pruned test-results of {len(pruned)} older run(s), kept {keep}")

        q_window = int(proj.get("quarantine_after") or 0)
        flaky_keys, quarantined = update_history(webqa, run_id, rows, q_window)
        stats, gate_ok = snapshot("final", flaky_keys, quarantined)

        if args.junit:
            junit_path = Path(args.junit)
            junit_path.parent.mkdir(parents=True, exist_ok=True)
            junit_path.write_text(render_junit_xml(proj["alias"], run_id, rows, quarantined))
            emit("matrix", f"junit → {junit_path}")

        # Zero-config CI summary: inside GitHub Actions the matrix lands on the run page
        gss = os.environ.get("GITHUB_STEP_SUMMARY")
        if gss:
            with open(gss, "a", encoding="utf-8") as f:
                f.write("\n" + matrix_md.read_text(encoding="utf-8"))

        print(json.dumps({"run_id": run_id, "gate_ok": gate_ok, "stats": stats,
                          "total": len(rows), "matrix": str(matrix_md)}, ensure_ascii=False))
        return 0 if gate_ok else 1
    finally:
        if not args.no_fixtures:
            run_fixture_cmd(proj, teardown=True)


if __name__ == "__main__":
    raise SystemExit(main())
