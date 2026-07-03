"""MD-driven scenario runner v2.

Improvements over v1:
  - Frontend vs backend path detection: paths mentioned with HTTP method (GET/POST/...) are
    treated as BACKEND only and excluded from frontend goto attempts.
  - Smart URL inference from TC body + title when no URL is in backticks. We look for tokens
    like /orders, /shipments, /admin/users in plain text too.
  - Smarter expected-keyword matching: word-boundary tokens (4+ chars), Cyrillic-aware,
    multiple variants per bullet.
  - axe-core injection on every visited frontend page → critical+serious violations recorded.
  - Better classification: GET-only TCs are passive even if they look "edit"-ish in prose
    when the only backend op is GET.

Output identical to v1 (report.md, results.json, screenshots, console.json) plus a11y.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, ConsoleMessage, Response

from explore import (
    api_login,
    context_kwargs_for,
    cookies_to_storage_state,
    load_project,
    resolve_credentials,
    viewport_entry,
    viewport_suffix,
)


def now_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# Patterns
RE_TC_HEADER = re.compile(r"^##\s+(TC-[A-Za-z0-9-]+)\s*[—-]\s*(.+?)(?:\s+\(.*?\))?$", re.MULTILINE)
RE_BACKEND_OP = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+`?(/[a-z][a-zA-Z0-9/_\-{}.]*)`?", re.IGNORECASE)
RE_PATH_BACKTICKED = re.compile(r"`(/[a-z][a-z0-9/_\-{}.]*)`", re.IGNORECASE)
RE_PATH_PLAIN = re.compile(r"(?<![A-Za-z0-9/])(/[a-z][a-z0-9/_\-{}.]*)(?![A-Za-z0-9/.])")
RE_MUTATION_HINT = re.compile(
    r"\bsubmit\b|сохрани|удали|создат|редакт|правк|изменен|drag|upload|загруз|кликнуть.*кнопк|ввес[тт]и",
    re.IGNORECASE,
)

# Fallback when the project config has no `backend_prefixes`. Deliberately minimal:
# per-project API paths belong in <project>/.web-qa/config.json.
DEFAULT_BACKEND_PREFIXES = ("/auth", "/api", "/health")


def split_tcs(md: str) -> list[dict]:
    tcs = []
    headers = list(RE_TC_HEADER.finditer(md))
    for i, h in enumerate(headers):
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(md)
        body = md[start:end]
        tcs.append({"id": h.group(1), "title": h.group(2).strip(), "body": body})
    return tcs


RE_TC_ROLE = re.compile(r"\*\*Roles?:\*\*\s*([^\n]+)", re.IGNORECASE)


def tc_roles(body: str) -> list[str]:
    """Roles a TC is declared for (`**Role:** viewer` / `**Roles:** admin, editor`).
    Empty list = role-agnostic, runs under any account."""
    m = RE_TC_ROLE.search(body)
    if not m:
        return []
    return [r.strip().strip("`").lower() for r in m.group(1).split(",") if r.strip()]


def is_backend_path(path: str, backend_prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p) for p in backend_prefixes)


def extract_paths(text: str, backend_prefixes: tuple[str, ...] = DEFAULT_BACKEND_PREFIXES) -> tuple[list[str], list[tuple[str, str]]]:
    """Returns (frontend_paths, backend_ops). Backend op detection has priority — its paths are
    excluded from frontend list."""
    backs: list[tuple[str, str]] = []
    seen_b: set[tuple[str, str]] = set()
    backend_only_paths: set[str] = set()
    for m in RE_BACKEND_OP.finditer(text):
        method = m.group(1).upper()
        path = m.group(2).rstrip(".,;:")
        key = (method, path)
        if key not in seen_b:
            seen_b.add(key)
            backs.append(key)
        backend_only_paths.add(path)

    fronts: list[str] = []
    seen_f: set[str] = set()
    # 1. Backticked paths (most reliable)
    for m in RE_PATH_BACKTICKED.finditer(text):
        p = m.group(1).rstrip(".,;:")
        if p in backend_only_paths or p in seen_f:
            continue
        if is_backend_path(p, backend_prefixes):
            continue
        seen_f.add(p)
        fronts.append(p)
    # 2. Plain paths in prose (fallback)
    for m in RE_PATH_PLAIN.finditer(text):
        p = m.group(1).rstrip(".,;:")
        if p in backend_only_paths or p in seen_f:
            continue
        if is_backend_path(p, backend_prefixes):
            continue
        if not re.match(r"^/[a-z][a-z0-9/_\-{}]*$", p):
            continue
        # Avoid super-deep paths from prose (likely false positives like `/path/to/file.png`)
        if p.count("/") > 4:
            continue
        seen_f.add(p)
        fronts.append(p)
    return fronts, backs


def infer_root_path(tc: dict, route_hints: list[dict]) -> str | None:
    """If TC has no extracted frontend path, infer from project route_hints
    (config.json: [{"path": "/orders", "keywords": ["заказ", ...]}, ...])."""
    haystack = (tc.get("title", "") + " " + tc.get("body", "")[:500]).lower()
    for hint in route_hints:
        if any(k.lower() in haystack for k in hint.get("keywords", [])):
            return hint["path"]
    return None


def classify(body: str, backend_ops: list[tuple[str, str]]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    for method, _ in backend_ops:
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            reasons.append(f"contains {method}")
    if RE_MUTATION_HINT.search(body):
        reasons.append("text mentions submit/upload/edit/etc")
    if reasons:
        return "mutating", reasons
    return "passive", []


def expected_keywords(body: str) -> list[str]:
    m = re.search(
        r"\*\*Expected[^*]*\*\*\s*(.+?)(?=\n##|\n\*\*Negative|\n\*\*Steps|\n\*\*Acceptance|$)",
        body, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return []
    block = m.group(1)
    items = re.findall(r"^[-*]\s+(.+)$", block, re.MULTILINE)
    return [it.strip() for it in items[:10]]


def keyword_to_search_terms(kw: str) -> list[str]:
    """Pull 1-3 representative terms from a bullet. Picks long Cyrillic / Latin words (4+ chars)."""
    cleaned = re.sub(r"[`*_<>]", " ", kw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # words 4+ chars, allow cyrillic & latin & digits
    words = re.findall(r"[\wа-яА-ЯёЁ]{4,}", cleaned)
    # Skip generic words
    stop = {"должен", "должна", "должны", "видны", "видно", "видна", "видно",
            "expected", "should", "table", "view", "mode", "page", "click", "page"}
    picked = [w.lower() for w in words if w.lower() not in stop][:4]
    return picked


def materialize_path(path: str, ids: dict) -> str:
    """Substitute {placeholder} tokens with ids discovered via id_discovery.
    {order_id}/{order} → ids["order"]; generic {id}/{N} → first discovered id."""
    if "{" not in path:
        return path

    def repl(m: re.Match) -> str:
        token = m.group(1)
        base = token[:-3] if token.endswith("_id") else token
        for key in (base, token):
            if ids.get(key):
                return str(ids[key])
        if token in ("id", "N") and ids:
            return str(next(iter(ids.values())))
        return m.group(0)

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, path)


def discover_ids(backend: str, cookies: dict, id_discovery: list[dict]) -> dict:
    """Fetch a sample entity id per config entry
    (config.json: [{"endpoint": "/orders", "key": "order"}, ...])."""
    out: dict = {}
    if not id_discovery:
        return out
    with httpx.Client(cookies=cookies, timeout=10) as cli:
        for spec in id_discovery:
            endpoint, key = spec["endpoint"], spec["key"]
            try:
                r = cli.get(f"{backend}{endpoint}")
                data = r.json()
                items = data if isinstance(data, list) else data.get("items") or data.get("results") or []
                if items and isinstance(items[0], dict):
                    out[key] = items[0].get("id") or items[0].get(f"{key}_id")
            except Exception:
                pass
    return out


# --- a11y ---

AXE_JS = (Path(__file__).parent / "axe.min.js").read_text() if (Path(__file__).parent / "axe.min.js").exists() else ""


def run_axe_audit(page) -> dict:
    if not AXE_JS:
        return {"error": "axe.min.js not present"}
    try:
        # axe is normally preloaded via context.add_init_script; inject only if missing
        if not page.evaluate("() => typeof window.axe !== 'undefined'"):
            page.add_script_tag(content=AXE_JS)
        result = page.evaluate("""async () => {
            const r = await window.axe.run(document, { resultTypes: ['violations'] });
            return r.violations.map(v => ({
                id: v.id, impact: v.impact, help: v.help, helpUrl: v.helpUrl,
                tags: v.tags, nodeCount: v.nodes.length,
                nodes: v.nodes.slice(0, 12).map(n => ({
                    target: n.target,
                    html: (n.html || '').slice(0, 220),
                    failureSummary: (n.failureSummary || '').slice(0, 220),
                    any: (n.any || []).slice(0, 2).map(c => ({
                        id: c.id, message: (c.message || '').slice(0, 200),
                        data: c.data || null,
                    })),
                })),
            }));
        }""")
        return {"violations": result}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def visual_diff_pct(baseline_path: Path, current_path: Path) -> float | str | None:
    """Return % of differing pixels (0..100), "size-mismatch", or None if can't compute."""
    try:
        from PIL import Image, ImageChops
        b = Image.open(baseline_path).convert("RGB")
        c = Image.open(current_path).convert("RGB")
        if b.size != c.size:
            # A changed viewport/page height IS a layout regression; resizing would smear it
            return "size-mismatch"
        diff = ImageChops.difference(b, c)
        bbox = diff.getbbox()
        if not bbox:
            return 0.0
        # count "different enough" pixels (any channel delta > 20 to ignore JPEG/AA noise)
        import numpy as np
        arr = np.asarray(diff)
        mask = (arr.max(axis=2) > 20)
        return 100.0 * mask.sum() / mask.size
    except Exception as e:
        print(f"[visual] diff failed: {e}", file=sys.stderr)
        return None


def apply_visual_masks(page, mask_selectors: list[str]) -> None:
    """Hide dynamic elements (clocks, counters, avatars) before screenshotting so
    they don't produce false visual regressions. Selectors come from config
    `visual_masks`. visibility:hidden keeps layout, so masked areas stay stable."""
    if not mask_selectors:
        return
    css = ", ".join(mask_selectors) + " { visibility: hidden !important; }"
    try:
        page.add_style_tag(content=css)
    except Exception as e:
        print(f"[visual] mask failed: {e}", file=sys.stderr)


def run_passive_tc(tc: dict, page, target: str, backend: str, cookies: dict, ids: dict, reports_dir: Path,  # noqa: PLR0913
                   baseline_dir: Path, update_baseline: bool, visual_threshold: float,
                   backend_prefixes: tuple[str, ...], route_hints: list[dict],
                   visual_masks: list[str], visual_exclude: list[str], vp_suffix: str = "") -> dict:
    body = tc["body"]
    fronts, backs = extract_paths(body, backend_prefixes)
    expected = expected_keywords(body)

    # Inference
    if not fronts:
        inferred = infer_root_path(tc, route_hints)
        if inferred:
            fronts = [inferred]

    notes: list[str] = []
    overall = "pass"
    artifacts: list[str] = []
    a11y_violations: list[dict] = []
    visual_results: list[dict] = []

    # Frontend
    for path in fronts[:3]:
        full = target.rstrip("/") + materialize_path(path, ids)
        try:
            page.goto(full, wait_until="domcontentloaded", timeout=18000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass  # SPA with polling never goes idle — bounded wait is enough
            visible_text = page.inner_text("body").lower()
            shot_key = (path.strip("/").replace("/", "_").replace("{", "_").replace("}", "_") or "root") + vp_suffix
            shot = reports_dir / f"{tc['id']}-{shot_key}.png"
            apply_visual_masks(page, visual_masks)
            page.screenshot(path=str(shot), full_page=False)
            artifacts.append(shot.name)

            # Visual regression. Data-driven pages (entity lists, live counters) make baselines
            # rot with every data change — config `visual_exclude` globs opt them out entirely
            # (screenshot still saved as an artifact); `visual_masks` is the softer option.
            import fnmatch
            baseline_file = baseline_dir / f"{shot_key}.png"
            if any(fnmatch.fnmatch(path, g) for g in visual_exclude):
                visual_results.append({"path": path, "status": "excluded-data-driven"})
            elif update_baseline:
                baseline_dir.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy(shot, baseline_file)
                visual_results.append({"path": path, "status": "baseline-updated", "file": str(baseline_file)})
            elif baseline_file.exists():
                pct = visual_diff_pct(baseline_file, shot)
                if pct is None:
                    visual_results.append({"path": path, "status": "diff-error"})
                elif pct == "size-mismatch":
                    visual_results.append({"path": path, "status": "regression", "diff_pct": "size-mismatch"})
                    overall = "fail"
                    notes.append(f"VISUAL regression on {path}: screenshot size differs from baseline")
                elif pct > visual_threshold:
                    visual_results.append({"path": path, "status": "regression", "diff_pct": pct})
                    overall = "fail"
                    notes.append(f"VISUAL regression on {path}: {pct:.2f}% pixels differ (threshold {visual_threshold}%)")
                else:
                    visual_results.append({"path": path, "status": "match", "diff_pct": pct})
            else:
                visual_results.append({"path": path, "status": "no-baseline"})

            # axe-core
            axe_res = run_axe_audit(page)
            if axe_res.get("violations"):
                criticals = [v for v in axe_res["violations"] if v.get("impact") in ("critical", "serious")]
                if criticals:
                    a11y_violations.extend([{**v, "path": path} for v in criticals])

            # Keyword matching against VISIBLE text only (markup/JSON in scripts doesn't count)
            if expected:
                hit_count = 0
                checkable = 0
                for kw in expected:
                    terms = keyword_to_search_terms(kw)
                    if not terms:
                        continue
                    checkable += 1
                    if any(t in visible_text for t in terms):
                        hit_count += 1
                notes.append(f"GOTO {path} → {hit_count}/{checkable} expected matched (visible text)")
                # pass needs ≥30% of checkable expected bullets visible on the page
                if checkable and hit_count / checkable < 0.3:
                    overall = "fail"
                    notes.append(f"  only {hit_count}/{checkable} expected-keywords visible on {path} (<30%)")
        except Exception as e:
            overall = "error"
            notes.append(f"GOTO {path} → exception: {str(e)[:120]}")

    # Backend GETs
    with httpx.Client(cookies=cookies, timeout=10) as cli:
        for method, path in backs[:5]:
            if method != "GET":
                continue
            full = backend.rstrip("/") + materialize_path(path, ids)
            try:
                r = cli.request(method, full)
                notes.append(f"{method} {path} → {r.status_code}")
                if r.status_code >= 400:
                    overall = "fail"
            except Exception as e:
                overall = "error"
                notes.append(f"{method} {path} → exception: {str(e)[:80]}")

    if not fronts and not [b for b in backs if b[0] == "GET"]:
        return {"id": tc["id"], "title": tc["title"], "kind": "passive", "status": "skip",
                "notes": ["no frontend path or GET backend op extracted (and no inference)"],
                "artifacts": artifacts, "a11y_critical": [], "visual": []}

    return {"id": tc["id"], "title": tc["title"], "kind": "passive", "status": overall,
            "notes": notes, "artifacts": artifacts, "a11y_critical": a11y_violations,
            "visual": visual_results}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    ap.add_argument("--email")
    ap.add_argument("--password")
    ap.add_argument("--role", help="named role from project config `roles` (RBAC runs)")
    ap.add_argument("--viewport", help="named viewport from config `viewports`; "
                                       "non-default gets its own baseline set (@name suffix)")
    ap.add_argument("--scenarios", help="Glob within .web-qa/scenarios/. Default: *.md")
    ap.add_argument("--include-mutating", action="store_true",
                    help="Attempt to run mutating TCs (placeholder; spec-gen not implemented)")
    ap.add_argument("--update-baseline", action="store_true",
                    help="Save current screenshots as the new visual baseline (no diff this run)")
    ap.add_argument("--visual-threshold", type=float, default=1.0,
                    help="Visual regression threshold in %% pixels-differ (default 1.0)")
    args = ap.parse_args()

    proj = load_project(args.alias)
    target = proj["target_url"]
    backend = proj.get("backend_url") or target
    project_path = Path(proj["path"])
    backend_prefixes = tuple(proj.get("backend_prefixes") or DEFAULT_BACKEND_PREFIXES)
    route_hints = proj.get("route_hints") or []
    id_discovery = proj.get("id_discovery") or []
    visual_masks = proj.get("visual_masks") or []
    visual_exclude = proj.get("visual_exclude") or []
    run_id = now_run_id() + (f"-{args.role}" if args.role else "") + (f"-{args.viewport}" if args.viewport else "")
    vp_suffix = viewport_suffix(proj, args.viewport)
    reports = project_path / ".web-qa" / "reports" / run_id
    reports.mkdir(parents=True, exist_ok=True)
    baseline_dir = project_path / ".web-qa" / "baseline"

    email, password = resolve_credentials(proj, args.email, args.password, args.role)
    cookies, user_me = api_login(backend, email, password)
    storage = cookies_to_storage_state(cookies, target)
    ids = discover_ids(backend, cookies, id_discovery)
    print(f"[run] discovered ids: {ids}", file=sys.stderr)

    scenarios_dir = project_path / ".web-qa" / "scenarios"
    pattern = args.scenarios or "*.md"
    scenario_files = sorted(scenarios_dir.glob(pattern))
    if not scenario_files:
        print(f"no scenarios found in {scenarios_dir}", file=sys.stderr)
        return 1

    console_log: list[dict] = []
    network_fails: list[dict] = []

    def on_console(msg: ConsoleMessage):
        console_log.append({"type": msg.type, "text": msg.text[:300]})

    def on_response(resp: Response):
        if resp.status >= 400 and "/__nextjs_original-stack-frames" not in resp.url:
            network_fails.append({"status": resp.status, "method": resp.request.method, "url": resp.url[:200]})

    all_results: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=storage,
                                  **context_kwargs_for(viewport_entry(proj, args.viewport), p))
        if AXE_JS:
            ctx.add_init_script(AXE_JS)
        page = ctx.new_page()
        page.on("console", on_console)
        page.on("response", on_response)

        for sf in scenario_files:
            md = sf.read_text()
            tcs = split_tcs(md)
            print(f"[run] {sf.name}: {len(tcs)} TC", file=sys.stderr)
            for tc in tcs:
                declared = tc_roles(tc["body"])
                if declared and (args.role or "").lower() not in declared:
                    hint = "" if args.role else " — run with --role"
                    all_results.append({
                        "id": tc["id"], "title": tc["title"], "kind": "role-specific",
                        "status": "skip",
                        "notes": [f"declared for role(s): {', '.join(declared)}{hint}"],
                        "artifacts": [], "scenario_file": sf.name, "a11y_critical": [],
                    })
                    continue
                fronts_pre, backs_pre = extract_paths(tc["body"], backend_prefixes)
                kind, reasons = classify(tc["body"], backs_pre)
                if kind == "mutating" and not args.include_mutating:
                    all_results.append({
                        "id": tc["id"], "title": tc["title"], "kind": "mutating",
                        "status": "manual", "notes": ["skipped (mutating); reasons: " + ", ".join(reasons)],
                        "artifacts": [], "scenario_file": sf.name, "a11y_critical": [],
                    })
                    continue
                if kind == "mutating":
                    all_results.append({
                        "id": tc["id"], "title": tc["title"], "kind": "mutating",
                        "status": "manual",
                        "notes": ["mutating — passive runner skips it; run via specs "
                                  "(web-qa-spec-gen + web-qa-run-specs / web-qa-matrix)"],
                        "artifacts": [], "scenario_file": sf.name, "a11y_critical": [],
                    })
                    continue

                res = run_passive_tc(tc, page, target, backend, cookies, ids, reports,
                                     baseline_dir, args.update_baseline, args.visual_threshold,
                                     backend_prefixes, route_hints, visual_masks, visual_exclude,
                                     vp_suffix)
                res["scenario_file"] = sf.name
                all_results.append(res)
                print(f"  {res['id']}: {res['status']} ({len(res.get('a11y_critical', []))} a11y critical)", file=sys.stderr)

        browser.close()

    # Aggregate
    by_status: dict[str, int] = {}
    for r in all_results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    a11y_total = sum(len(r.get("a11y_critical", [])) for r in all_results)
    a11y_unique = {(v.get("id"), v.get("path")) for r in all_results for v in r.get("a11y_critical", [])}

    lines = [
        f"# Full Regression v2 — {proj['alias']}",
        f"\n_Run: {run_id} UTC_",
        f"\n- Target: {target}",
        f"- Backend: {backend}",
        f"- Logged-in: {user_me.get('email')} ({user_me.get('role')})" + (f" [role: {args.role}]" if args.role else ""),
        f"- Discovered ids: {ids}",
        f"- Total TC: {len(all_results)}",
    ]
    for st, n in sorted(by_status.items()):
        emoji = {"pass": "✅", "fail": "❌", "error": "⚠️", "manual": "✋", "skip": "⏭"}.get(st, "?")
        lines.append(f"- {emoji} {st}: {n}")
    lines.append(f"\n**a11y critical/serious violations:** {a11y_total} total, {len(a11y_unique)} unique (id+page)")
    lines.append("\n## TC Results\n")
    lines.append("| File | TC | Kind | Status | A11y | Title | Notes |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in all_results:
        notes = " · ".join(r.get("notes", []))[:280].replace("|", "\\|")
        emoji = {"pass": "✅", "fail": "❌", "error": "⚠️", "manual": "✋", "skip": "⏭"}.get(r["status"], "?")
        a11y_cnt = len(r.get("a11y_critical", []))
        a11y_emoji = "♿" + ("✅" if a11y_cnt == 0 else "❌") + (f"({a11y_cnt})" if a11y_cnt else "")
        lines.append(f"| {r['scenario_file']} | {r['id']} | {r['kind']} | {emoji} {r['status']} | {a11y_emoji} | {r['title'][:55]} | {notes} |")

    lines.append(f"\n**Network failures (4xx/5xx, filtered):** {len(network_fails)}")
    if network_fails[:10]:
        lines.append("\n### Sample network failures")
        for nf in network_fails[:10]:
            lines.append(f"- `{nf['method']} {nf['url']}` → {nf['status']}")
    console_errs = sum(1 for c in console_log if c["type"] in ("error", "warning"))
    lines.append(f"\n**Console errors/warnings:** {console_errs}")

    if a11y_total:
        lines.append("\n## A11y violations\n")
        for r in all_results:
            for v in r.get("a11y_critical", []):
                lines.append(f"- `{v.get('path')}` [{v.get('impact')}] **{v.get('id')}** — {v.get('help')} ({v.get('nodeCount')} nodes)")

    (reports / "report.md").write_text("\n".join(lines) + "\n")
    (reports / "results.json").write_text(json.dumps({
        "run_id": run_id,
        "role": args.role,
        "viewport": args.viewport,
        "by_status": by_status,
        "a11y_total": a11y_total,
        "results": all_results,
        "network_failures": network_fails,
    }, ensure_ascii=False, indent=2))
    (reports / "console.json").write_text(json.dumps(console_log, ensure_ascii=False, indent=2))
    (reports / "a11y.json").write_text(json.dumps([
        {"tc": r["id"], "violations": r.get("a11y_critical", [])}
        for r in all_results if r.get("a11y_critical")
    ], ensure_ascii=False, indent=2))

    visual_regressions = sum(
        1 for r in all_results
        for v in r.get("visual", []) if v.get("status") == "regression"
    )

    print(json.dumps({
        "run_id": run_id,
        "by_status": by_status,
        "a11y_total": a11y_total,
        "visual_regressions": visual_regressions,
        "report": str(reports / "report.md"),
        "total": len(all_results),
    }, ensure_ascii=False))
    return 0




if __name__ == "__main__":
    raise SystemExit(main())
