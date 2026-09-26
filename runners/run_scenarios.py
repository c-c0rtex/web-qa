"""MD-driven scenario runner v2.

Improvements over v1:
  - Frontend vs backend path detection: paths mentioned with HTTP method (GET/POST/...) are
    treated as BACKEND only and excluded from frontend goto attempts.
  - Smart URL inference from TC body + title when no URL is in backticks. We look for tokens
    like /orders, /shipments, /admin/users in plain text too.
  - Smarter expected-keyword matching: word-boundary tokens (4+ chars), Cyrillic-aware,
    multiple variants per bullet.
  - axe-core injection on every visited frontend page → critical+serious violations recorded.
  - Language-agnostic classification: the declared `**Type:**` field is the source of truth;
    no natural-language keyword matching, so TCs written in any language classify identically.

Output identical to v1 (report.md, results.json, screenshots, console.json) plus a11y.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, ConsoleMessage, Response

from progress import Progress, emit

from entity_ids import backend_client, discover_ids, id_routes, materialize_path  # noqa: F401
from explore import (
    api_login,
    build_storage_state,
    context_kwargs_for,
    load_project,
    resolve_credentials,
    run_fixture_cmd,
    viewport_entry,
    viewport_suffix,
)


def now_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# Patterns
RE_TC_HEADER = re.compile(r"^##\s+(TC-[A-Za-z0-9-]+)\s*[—-]\s*(.+?)(?:\s+\(.*?\))?$", re.MULTILINE)
RE_BACKEND_OP = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+`?(/[a-z][a-zA-Z0-9/_\-{}.]*)`?", re.IGNORECASE)
RE_PATH_BACKTICKED = re.compile(r"`(/(?:[a-z][a-z0-9/_\-{}.]*)?(?:\?[^`\s]*)?)`", re.IGNORECASE)
RE_PATH_PLAIN = re.compile(r"(?<![A-Za-z0-9/])(/[a-z][a-z0-9/_\-{}.]*)(?![A-Za-z0-9/.])")
RE_TC_TYPE = re.compile(r"\*\*Type:\*\*\s*`?(passive|mutating)`?", re.IGNORECASE)
MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")

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


RE_STEPS = re.compile(r"\*\*Steps?:\*\*(.*?)(?=\*\*Expected|\Z)", re.S | re.IGNORECASE)


def norm_route(path: str) -> str:
    """Collapse a concrete path onto the template shape the app map uses."""
    r = re.sub(r"\{[^}]+\}", "{id}", path.split("?")[0]) or "/"
    return re.sub(r"/\d+(?=/|$)", "/{id}", r)


def tc_routes(body: str) -> set[str]:
    """Routes a test case actually navigates to.

    Steps only, and never the target of an HTTP verb. Both restrictions are load-bearing:
    an Expected bullet reading "redirects to `/`" names a route the TC never exercises,
    and a step that documents its own `GET /admin/users` still visits that page."""
    m = RE_STEPS.search(body)
    if not m:
        return set()
    steps = RE_BACKEND_OP.sub(" ", m.group(1))   # strip `GET /x` API references
    return {norm_route(hit.group(1).rstrip(".,;:"))
            for hit in RE_PATH_BACKTICKED.finditer(steps)}


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
    # 1. Backticked paths (most reliable). Query strings collapse to the route itself,
    # so the bare root — `/` or `/?limit=10&offset=0` — is extractable like any path.
    for m in RE_PATH_BACKTICKED.finditer(text):
        p = m.group(1).rstrip(".,;:").split("?")[0] or "/"
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


def declared_type(body: str) -> str | None:
    """Explicit `**Type:** passive|mutating` from the TC body. The field values are fixed
    format vocabulary (like `**Role:**`), independent of the language steps are written in."""
    m = RE_TC_TYPE.search(body)
    return m.group(1).lower() if m else None


RE_STEPS_BLOCK = re.compile(r"\*\*Steps:?\*\*\s*(.+?)(?=\n\*\*|\n##|$)", re.DOTALL | re.IGNORECASE)


def steps_mutating_ops(body: str) -> list[tuple[str, str]]:
    """Mutating HTTP ops from the **Steps:** block only. Steps are the test's ACTIONS —
    a method mentioned in **Expected:** («data comes from POST /sync») is context, not
    something the test does, so it is never evidence of mutation."""
    m = RE_STEPS_BLOCK.search(body)
    scope = m.group(1) if m else body  # freeform TC without a Steps block: best effort
    return [(op.group(1).upper(), op.group(2))
            for op in RE_BACKEND_OP.finditer(scope)
            if op.group(1).upper() in MUTATING_METHODS]


def classify(body: str) -> tuple[str, list[str]]:
    """Language-agnostic: structural evidence beats the declaration, the declaration beats
    absence — never prose keywords. A declared `passive` is overridden when Steps contain a
    mutating HTTP op (an LLM mislabel must not earn a green passive check for actions the
    passive runner never executes); a TC with no signals at all is treated as mutating for
    the same reason."""
    t = declared_type(body)
    ops = steps_mutating_ops(body)
    if t == "passive":
        if ops:
            return "mutating", [f"declared passive contradicted by {m} {p} in Steps" for m, p in ops]
        return "passive", []
    if t == "mutating":
        return "mutating", ["declared **Type:** mutating"]
    reasons = [f"contains {m} {p}" for m, p in ops]
    reasons.append("no **Type:** declared — add `**Type:** passive` to run it in the passive stage")
    return "mutating", reasons


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


RE_QUOTED_UI_TEXT = re.compile(r"`([^`]{2,60})`|«([^»]{2,60})»|“([^”]{2,60})”|\"([^\"]{2,60})\"")
# CJK scripts pack a word into 1-3 chars, so the 4+ threshold below would drop them entirely
RE_CJK_TOKEN = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]{2,}")


def keyword_to_search_terms(kw: str) -> list[str]:
    """Representative search terms from an Expected bullet, language-agnostic:
    quoted/backticked strings are taken verbatim first (exact UI text beats word heuristics
    in any language), then Unicode word tokens (`\\w` covers all scripts)."""
    terms: list[str] = [next(g for g in m.groups() if g).strip().lower()
                        for m in RE_QUOTED_UI_TEXT.finditer(kw)]
    cleaned = re.sub(r"[`*_<>«»“”\"]", " ", kw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    words = re.findall(r"\w{4,}", cleaned) + RE_CJK_TOKEN.findall(cleaned)
    # Skip generic words (best-effort noise filter, not a classification signal)
    stop = {"должен", "должна", "должны", "видны", "видно", "видна",
            "expected", "should", "table", "view", "mode", "page", "click"}
    terms += [w.lower() for w in words if w.lower() not in stop]
    seen: set[str] = set()
    return [t for t in terms if t and not (t in seen or seen.add(t))][:6]


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
        emit("visual", f"diff failed: {e}")
        return None


def gated_console(entries: list[dict], fail_on: list[str], ignore: list[str]) -> list[dict]:
    """Console entries (captured this TC) that should FAIL the TC: type in `fail_on`
    and text not matching any `ignore` regex. Empty `fail_on` = gating off (opt-in)."""
    if not fail_on:
        return []
    pats = [re.compile(p) for p in ignore]
    return [c for c in entries
            if c.get("type") in fail_on
            and not any(p.search(c.get("text", "")) for p in pats)]


def save_diff_mask(baseline_path: Path, current_path: Path, out_path: Path) -> bool:
    """Render WHERE the pixels differ (red on white) — reviewing a visual regression means
    Reading baseline, current and this mask side by side, not staring at a percentage."""
    try:
        import numpy as np
        from PIL import Image, ImageChops
        b = Image.open(baseline_path).convert("RGB")
        c = Image.open(current_path).convert("RGB")
        if b.size != c.size:
            return False
        arr = np.asarray(ImageChops.difference(b, c))
        mask = arr.max(axis=2) > 20
        out = np.full((*mask.shape, 3), 255, dtype="uint8")
        out[mask] = (220, 30, 30)
        Image.fromarray(out).save(out_path)
        return True
    except Exception as e:
        emit("visual", f"diff mask failed: {e}")
        return False


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
        emit("visual", f"mask failed: {e}")


def spec_for_tc(webqa: Path, tc_id: str) -> str | None:
    """The generated spec that executes this test case, if one exists.

    Two shapes, because the file name is `<scenario>__<slug(TC-ID + title)>.spec.ts` and a
    title written entirely in a non-latin script slugifies to nothing: `…__tc-gap7.spec.ts`
    alongside `…__tc-adm2--scope-level.spec.ts`. Anchoring on `tc-gap7-` alone missed every
    Cyrillic-titled test case — the ones this project is full of.

    Kept local (a glob, not an import) because spec_gen imports THIS module."""
    slug = tc_id.lower()
    specs = webqa / "specs"
    hits = sorted(specs.glob(f"*__{slug}.spec.ts")) + sorted(specs.glob(f"*__{slug}-*.spec.ts"))
    return hits[0].name if hits else None


def run_passive_tc(tc: dict, page, target: str, backend: str, cookies: dict, ids: dict, reports_dir: Path,  # noqa: PLR0913
                   baseline_dir: Path, update_baseline: bool, visual_threshold: float,
                   backend_prefixes: tuple[str, ...], route_hints: list[dict],
                   visual_masks: list[str], visual_exclude: list[str], vp_suffix: str = "",
                   update_routes: str | None = None, token: str | None = None,
                   asserted_by_spec: str | None = None,
                   entity_routes: dict[str, str] | None = None) -> dict:
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
    probed = 0

    # Frontend
    for path in fronts[:3]:
        materialized = materialize_path(path, ids, entity_routes)
        if "{" in materialized:
            # No id for this template (see materialize_path). Opening `/people/{id}` literally
            # measures a 404 page against the test case's expectations — not the app.
            notes.append(f"GOTO {path} → skipped: unresolved placeholder "
                         f"(add an `id_discovery` entry with `route` in .web-qa/config.json)")
            continue
        full = target.rstrip("/") + materialized
        probed += 1
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
            elif update_baseline and (not update_routes or fnmatch.fnmatch(path, update_routes)):
                baseline_dir.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy(shot, baseline_file)
                visual_results.append({"path": path, "status": "baseline-updated", "file": str(baseline_file)})
            elif baseline_file.exists():
                pct = visual_diff_pct(baseline_file, shot)
                if pct is None:
                    visual_results.append({"path": path, "status": "diff-error"})
                elif pct == "size-mismatch":
                    visual_results.append({"path": path, "status": "regression", "diff_pct": "size-mismatch",
                                           "baseline": str(baseline_file), "current": shot.name})
                    overall = "fail"
                    notes.append(f"VISUAL regression on {path}: screenshot size differs from baseline")
                elif pct > visual_threshold:
                    diff_shot = reports_dir / f"{tc['id']}-{shot_key}-diff.png"
                    entry = {"path": path, "status": "regression", "diff_pct": pct,
                             "baseline": str(baseline_file), "current": shot.name}
                    if save_diff_mask(baseline_file, shot, diff_shot):
                        entry["diff_image"] = diff_shot.name
                        artifacts.append(diff_shot.name)
                    visual_results.append(entry)
                    overall = "fail"
                    notes.append(f"VISUAL regression on {path}: {pct:.2f}% pixels differ (threshold {visual_threshold}%)"
                                 + " — review baseline vs current vs the diff mask, then accept with"
                                 f" --update-baseline --routes '{path}' if intentional")
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
                # A weak oracle must never veto a strong one. This check counts how many words
                # of the Expected prose appear on the page. The better a test case gets — "the
                # row count equals the number of live orders per GET /orders" — the fewer of
                # its words a page can possibly show. Once a spec asserts the same test case
                # with real assertions, the keyword score is a smoke signal, not a verdict.
                if checkable and hit_count / checkable < 0.3:
                    if asserted_by_spec:
                        notes.append(f"  only {hit_count}/{checkable} expected-keywords visible on "
                                     f"{path} — informational: asserted by `specs/{asserted_by_spec}`")
                    else:
                        overall = "fail"
                        notes.append(f"  only {hit_count}/{checkable} expected-keywords visible on {path} (<30%)")
        except Exception as e:
            overall = "error"
            notes.append(f"GOTO {path} → exception: {str(e)[:120]}")

    # Backend GETs
    with backend_client(cookies, token) as cli:
        for method, path in backs[:5]:
            if method != "GET":
                continue
            materialized = materialize_path(path, ids, entity_routes)
            if "{" in materialized:
                # `id_discovery` is unset or returned nothing, so `/orders/{order_id}` is being
                # requested literally. A 404 for a URL that was never a URL is not the app
                # failing — it is us probing a template.
                notes.append(f"{method} {path} → skipped: unresolved placeholder "
                             f"(set `id_discovery` in .web-qa/config.json)")
                continue
            full = backend.rstrip("/") + materialized
            probed += 1
            try:
                r = cli.request(method, full)
                notes.append(f"{method} {path} → {r.status_code}")
                if r.status_code >= 400:
                    overall = "fail"
            except Exception as e:
                overall = "error"
                notes.append(f"{method} {path} → exception: {str(e)[:80]}")

    if not probed:
        # Nothing was opened or requested — a "pass" here would certify an unchecked case.
        return {"id": tc["id"], "title": tc["title"], "kind": "passive", "status": "skip",
                "notes": notes or ["no frontend path or GET backend op extracted (and no inference)"],
                "artifacts": artifacts, "a11y_critical": [], "visual": []}

    return {"id": tc["id"], "title": tc["title"], "kind": "passive", "status": overall,
            "notes": notes, "artifacts": artifacts, "a11y_critical": a11y_violations,
            "visual": visual_results}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    ap.add_argument("--email")
    ap.add_argument("--password")
    ap.add_argument("--reports-dir", help="write the report into this directory instead of "
                                          ".web-qa/reports/<run-id> (used by web-qa-matrix so "
                                          "one run leaves one folder, not two siblings)")
    ap.add_argument("--role", help="named role from project config `roles` (RBAC runs)")
    ap.add_argument("--viewport", help="named viewport from config `viewports`; "
                                       "non-default gets its own baseline set (@name suffix)")
    ap.add_argument("--scenarios", help="Glob within .web-qa/scenarios/. Default: *.md")
    ap.add_argument("--update-baseline", action="store_true",
                    help="Save current screenshots as the new visual baseline (no diff this run)")
    ap.add_argument("--routes", help="glob limiting --update-baseline to matching routes "
                                     "(accept one reviewed change, not everything at once)")
    ap.add_argument("--visual-threshold", type=float, default=1.0,
                    help="Visual regression threshold in %% pixels-differ (default 1.0)")
    ap.add_argument("--no-fixtures", action="store_true",
                    help="skip the project's fixture_cmd for this run")
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
    reports = Path(args.reports_dir) if args.reports_dir else project_path / ".web-qa" / "reports" / run_id
    reports.mkdir(parents=True, exist_ok=True)
    baseline_dir = project_path / ".web-qa" / "baseline"

    if not args.no_fixtures:
        run_fixture_cmd(proj)

    email, password = resolve_credentials(proj, args.email, args.password, args.role)
    cookies, user_me, token = api_login(backend, email, password, proj)
    storage = build_storage_state(cookies, target, token, proj)
    ids = discover_ids(backend, cookies, id_discovery, token)
    emit("run", f"discovered ids: {ids}")

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

        total_tcs = sum(len(split_tcs(sf.read_text())) for sf in scenario_files)
        bar = Progress("run", total_tcs)
        bar.start(f"{len(scenario_files)} scenario file(s), {total_tcs} TC"
                  + (f", role={args.role}" if args.role else ""))
        for sf in scenario_files:
            md = sf.read_text()
            tcs = split_tcs(md)
            bar.note(f"{sf.name}: {len(tcs)} TC")
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
                    bar.step(f"{tc['id']}  role-specific", "SKIP")
                    continue
                kind, reasons = classify(tc["body"])
                if kind == "mutating":
                    # Not a skip you can turn off: this runner navigates and reads. Mutating
                    # test cases are EXECUTED, as playwright specs. Saying only "manual" made
                    # a done job look undone, so name the spec that does it — or its absence.
                    spec = spec_for_tc(project_path / ".web-qa", tc["id"])
                    note = (f"mutating — executed as `specs/{spec}`"
                            if spec else
                            "mutating — NO spec exists yet: run web-qa-spec-gen")
                    all_results.append({
                        "id": tc["id"], "title": tc["title"], "kind": "mutating",
                        "status": "manual", "spec": spec,
                        "notes": [note + "; reasons: " + ", ".join(reasons)],
                        "artifacts": [], "scenario_file": sf.name, "a11y_critical": [],
                    })
                    bar.step(f"{tc['id']}  {note}", "SPEC" if spec else "MAN")
                    continue

                nf_start = len(network_fails)
                console_start = len(console_log)
                res = run_passive_tc(tc, page, target, backend, cookies, ids, reports,
                                     baseline_dir, args.update_baseline, args.visual_threshold,
                                     backend_prefixes, route_hints, visual_masks, visual_exclude,
                                     vp_suffix, args.routes, token,
                                     spec_for_tc(project_path / ".web-qa", tc["id"]),
                                     id_routes(id_discovery))
                # Network assertion: a 5xx during THIS TC's navigation is a failure signal,
                # not a footnote (config `network_fail_on`, default ["5xx"] — add "4xx" to
                # tighten). Structural, language-agnostic, same as everywhere else.
                classes = proj.get("network_fail_on") or ["5xx"]
                bad = [nf for nf in network_fails[nf_start:]
                       if ("5xx" in classes and 500 <= nf["status"] < 600)
                       or ("4xx" in classes and 400 <= nf["status"] < 500)]
                if bad and res["status"] == "pass":
                    res["status"] = "fail"
                    res["notes"].append("NETWORK: " + "; ".join(
                        f"{nf['status']} {nf['method']} {nf['url'][:100]}" for nf in bad[:5]))
                # Console assertion (opt-in): a console message of a gated type during THIS
                # TC's navigation fails it. Off by default (`console_fail_on` empty) because
                # real apps are noisy; `console_ignore` regexes drop known third-party noise.
                bad_console = gated_console(console_log[console_start:],
                                            proj.get("console_fail_on") or [],
                                            proj.get("console_ignore") or [])
                if bad_console and res["status"] == "pass":
                    res["status"] = "fail"
                    res["notes"].append("CONSOLE: " + "; ".join(
                        f"[{c['type']}] {c['text'][:100]}" for c in bad_console[:5]))
                res["scenario_file"] = sf.name
                all_results.append(res)
                a11y = len(res.get("a11y_critical", []))
                bar.step(f"{res['id']}  {res['title'][:50]}"
                         + (f"  ({a11y} a11y critical)" if a11y else ""),
                         res["status"].upper()[:4])

        bar.finish()
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

    review = [(r["id"], v) for r in all_results
              for v in r.get("visual", []) if v.get("status") == "regression"]
    if review:
        lines.append("\n## Visual review queue\n")
        lines.append("Read all three images per row and judge; accept an intentional change with "
                     "`--update-baseline --routes '<route>'`.\n")
        for tc_id, v in review:
            triple = f"baseline `{v.get('baseline', '?')}` · current `{v.get('current', '?')}`"
            if v.get("diff_image"):
                triple += f" · diff `{v['diff_image']}`"
            lines.append(f"- {tc_id} `{v['path']}` — {v.get('diff_pct')}: {triple}")

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

    if not args.no_fixtures:
        run_fixture_cmd(proj, teardown=True)

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
