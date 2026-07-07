"""Generate Playwright .spec.ts files from .web-qa/scenarios/*.md test cases.

For each TC not declared `Type: passive` (or any TC if --all), build a prompt with
the TC content + project context, call `claude -p` once, save the output as
.web-qa/specs/<scenario-stem>__<tc-id>.spec.ts. Caches by sha256 of TC body to
skip unchanged TCs on rerun.

Usage:
  python3 spec_gen.py --alias my-app
  python3 spec_gen.py --alias my-app --all          # generate all TCs not just mutating
  python3 spec_gen.py --alias my-app --tc TC-I4     # one TC
  python3 spec_gen.py --alias my-app --force        # ignore cache

Output layout:
  <project>/.web-qa/specs/<scenario-stem>__<tc-id>.spec.ts
  <project>/.web-qa/specs/.cache.json            # {tc_key: sha256_hash}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from explore import load_project, resolve_credentials
from run_scenarios import declared_type, split_tcs, tc_roles


MAX_CONTEXT_CHARS = 24000
GEN_ATTEMPTS = 2  # initial + one retry with playwright parse error fed back

DEFAULT_AUTH_HINT = (
    "POST {backend_url}/auth/login with JSON {email, password}; inspect the response — "
    "if it sets cookies, they land in the browser context automatically when you login via "
    "page.request.post; if it returns a token, send it as `Authorization: Bearer <token>` "
    "on API requests."
)

PROMPT_TEMPLATE = """You are generating a Playwright TypeScript test for an existing {stack} app.

PROJECT CONTEXT:
- Frontend base URL: {frontend_url}
- Backend API base URL: {backend_url}
- Test credentials: email="{login_email}" password="{login_password}"
- AUTH FLOW (follow EXACTLY, do not invent cookies or headers): {auth_login_hint}
{seed_section}

APP MAP (auto-crawled; REAL routes, form fields, button labels and table headers — trust it over guesses):
{app_context}
{dnd_section}

TASK: Generate ONE Playwright spec file (TypeScript) for the test case below. Output ONLY raw .spec.ts code (no markdown fences, no commentary). The file will be saved verbatim and compiled by tsc.

REQUIREMENTS:
- Use `import {{ test, expect }} from '@playwright/test'` at the top
- Use a single `test('TC-XXX: <name>', async ({{ page, context }}) => {{ ... }})` block
- Authenticate at the start of the test following the AUTH FLOW above verbatim
- Convert each step in the TC to one or more Playwright actions
- Add `expect(...)` assertions covering the Expected bullets
- Selector priority: 1) `getByTestId` when the APP MAP shows a data-testid for the element,
  2) `getByRole` with the accessible name (the ARIA snapshots in the APP MAP are ground truth
  for role/name), 3) `getByLabel` / `getByText`. NEVER CSS classes, XPath or positional nth()
- Selector texts/labels MUST come from the APP MAP above when the route is listed there — do not invent button captions
- For navigation ALWAYS: `await page.goto('{frontend_url}<path>', {{ waitUntil: 'domcontentloaded' }})` —
  never the default 'load' and never 'networkidle': Next.js dev keeps an HMR websocket open, so
  those states never settle and the test burns its whole timeout
- NEVER use `waitForLoadState('networkidle')` anywhere. Web-first assertions
  (`await expect(locator).toBeVisible()`) auto-wait and are the correct sync point; for
  navigation waits use `page.waitForURL(...)`
- For backend assertions: `const r = await page.request.get(...); expect(r.status()).toBe(200)`
- Mock EXTERNAL third-party dependencies only (payments, outside APIs). NEVER mock or stub
  your own app's backend — the test must exercise the real stack
- Do not hardcode IDs — use `?` if TC is generic about which entity, or pick a plausible id
- Keep the spec self-contained; no external helpers

UI-FIRST RULE (what makes the spec worth anything):
- Every user-visible step of the TC MUST be performed through the UI — real clicks on real
  buttons, real form fills, real navigation — exactly as a user would do it
- Doing the action under test via `page.request` instead of the UI is a SPEC BUG: the test
  goes green while the actual user path may be broken
- `page.request` / API calls are allowed ONLY for: authentication, creating/deleting test
  data (setup/teardown), and side-verification of state AFTER a UI action

MUTATING DATA POLICY (applies when the TC creates/edits/deletes data):
- NEVER mutate pre-existing data. The test must create its OWN target entity via the backend API
  at the start (names/numbers prefixed "{test_data_prefix}"), act on it, assert, and delete it
  in a try/finally so cleanup runs even on assertion failure
- If the entity cannot be created via API, mark the risky step with `test.skip()` and a comment

TEST CASE:
{tc_body}

Output the .spec.ts code now (just the code, no fences):
"""

RETRY_SUFFIX = """

YOUR PREVIOUS ATTEMPT FAILED — playwright could not parse the generated file:
{error}

Output the FULL corrected .spec.ts (just the code, no fences):
"""

PROBE_RETRY_SUFFIX = """

A LIVE LOCATOR CHECK ran your previous attempt's locators against the RUNNING app.
Problems found on the spec's entry page:
{feedback}

Fix these locators using labels from the APP MAP / ARIA snapshots above — they are the
ground truth. EXCEPTION: if an element only appears after an interaction (a dialog, a
later step of the flow), it cannot exist on the entry page — keep such locators as they
are. Output the FULL corrected .spec.ts (just the code, no fences):
"""


def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", s).strip("-").lower()
    return s[:60] or "tc"


# --- drag-and-drop interaction recipes --------------------------------------------
# There is no universal "drag": HTML5 native DnD fires drag/drop events, while JS libs
# (dnd-kit, react-beautiful-dnd, SortableJS, interact.js) are pointer/mouse driven and
# need intermediate moves that cross an activation threshold — a single high-level
# dragAndDrop often silently no-ops on them. So we detect the library from the app's
# deps and hand the generator the exact, proven sequence for it.

DND_DEPS = {
    "@dnd-kit/core": "dnd-kit", "@dnd-kit/sortable": "dnd-kit",
    "react-beautiful-dnd": "react-beautiful-dnd", "@hello-pangea/dnd": "react-beautiful-dnd",
    "react-dnd": "react-dnd", "sortablejs": "sortablejs", "vuedraggable": "sortablejs",
    "interactjs": "interact.js", "react-draggable": "pointer", "@shopify/draggable": "pointer",
}

# Pointer-based helper: down on source, a small move to trip the sensor's activation
# distance, travel to target in steps, settle, up. Works for dnd-kit / rbd / SortableJS /
# interact.js and any custom pointer DnD.
_POINTER_HELPER = """async function dragTo(page, source, target) {
  const s = (await source.boundingBox())!;
  const t = (await target.boundingBox())!;
  const sx = s.x + s.width / 2, sy = s.y + s.height / 2;
  const tx = t.x + t.width / 2, ty = t.y + t.height / 2;
  await page.mouse.move(sx, sy);
  await page.mouse.down();
  await page.mouse.move(sx + 8, sy + 8, { steps: 5 });   // cross the sensor activation threshold
  await page.mouse.move(tx, ty, { steps: 12 });          // travel to the drop target
  await page.mouse.move(tx, ty, { steps: 3 });           // settle so the drop registers
  await page.mouse.up();
}"""

_NATIVE_HELPER = """// Native HTML5 drag-and-drop (elements with draggable="true"): Playwright's
// high-level dragTo drives the dragstart/dragover/drop events for you.
async function dragTo(page, source, target) {
  await source.dragTo(target);
}"""


_DND_SKIP_DIRS = {"node_modules", ".next", ".git", ".venv", "dist", "build", ".turbo", "coverage"}


def _dnd_from_pkg(pkg: Path) -> str | None:
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    for dep, lib in DND_DEPS.items():
        if dep in deps:
            return lib
    return None


def detect_dnd_library(frontend_dir: Path, max_depth: int = 3) -> str | None:
    """DnD library name from the frontend's package.json deps, or None. Checks the dir
    itself first, then walks up to `max_depth` levels down (skipping node_modules/.next/…)
    so a nested `app/frontend/package.json` monorepo layout is still found."""
    direct = _dnd_from_pkg(frontend_dir / "package.json")
    if direct:
        return direct
    if not frontend_dir.is_dir():
        return None
    for pkg in sorted(frontend_dir.rglob("package.json"), key=lambda p: len(p.parts)):
        if set(pkg.parts) & _DND_SKIP_DIRS:
            continue
        if len(pkg.relative_to(frontend_dir).parts) - 1 > max_depth:
            continue
        lib = _dnd_from_pkg(pkg)
        if lib:
            return lib
    return None


def dnd_recipe_section(lib: str | None) -> str:
    """Prompt section with the exact drag helper for the detected library. Empty when no
    DnD library is present (most apps) — keeps the prompt lean."""
    if not lib:
        return ""
    helper = _NATIVE_HELPER if lib == "native" else _POINTER_HELPER
    return f"""
DRAG-AND-DROP (this app uses **{lib}**) — if the test case involves dragging, reordering
or dropping, follow this recipe:
- Define this helper INSIDE the spec file and use it (do not invent your own drag sequence):
{helper}
- Locate the source and drop-target with getByTestId / getByRole from the APP MAP (drag
  handles and drop zones are marked there when present).
- Assert the RESULT via the DOM, not visually: the new ORDER of list items
  (`await expect(list.getByRole('listitem')).toHaveText([...])`) or CONTAINMENT
  (`await expect(target.getByText('Card A')).toBeVisible()`). Never assert a drag via a
  screenshot — positions shift and visual diff is noise here.
- Drag-and-drop is inherently flaky; if the drop doesn't register, add one more
  `await page.mouse.move(tx, ty, {{ steps: 3 }})` before `mouse.up()`.
"""


def tc_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def is_mutating(tc: dict) -> bool:
    """A TC is spec-worthy unless it explicitly declares `**Type:** passive`.
    Language-agnostic: only the structured field counts, never prose keywords —
    an undeclared TC gets a real spec rather than being silently skipped."""
    return declared_type(tc.get("body", "")) != "passive"


def call_claude(prompt: str, timeout: int | None = None) -> str:
    """Call `claude -p <prompt>` and return stdout.
    WEBQA_CLAUDE_MODEL (e.g. "sonnet", "opus") overrides the CLI default model.
    WEBQA_GEN_TIMEOUT (seconds, default 300) bounds one generation — complex multi-step
    TCs did not fit the old 180s and died silently."""
    timeout = timeout or int(os.environ.get("WEBQA_GEN_TIMEOUT", "300"))
    cmd = ["claude", "-p", prompt]
    model = os.environ.get("WEBQA_CLAUDE_MODEL")
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {proc.stderr[:500]}")
    out = proc.stdout.strip()
    # Strip code fences if model added them despite instructions
    out = re.sub(r"^```(?:typescript|ts)?\s*\n?", "", out)
    out = re.sub(r"\n?```\s*$", "", out)
    return out


def postprocess_spec(code: str) -> str:
    """Mechanical safety net over LLM output — the prompt forbids networkidle, but if it
    slips through anyway, downgrade it to a state that actually settles."""
    return code.replace("'networkidle'", "'domcontentloaded'").replace('"networkidle"', '"domcontentloaded"')


def validate_spec(webqa: Path, out_path: Path) -> str | None:
    """Ask playwright to parse the spec without running it. None = OK, str = error text.
    Skips silently (accept) when the project has no local playwright install."""
    if not (webqa / "node_modules" / "@playwright" / "test").is_dir():
        return None
    proc = subprocess.run(
        ["npx", "playwright", "test", "--list", str(out_path.relative_to(webqa))],
        cwd=webqa, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode == 0:
        return None
    return (proc.stderr or proc.stdout)[-1500:]


def load_app_context(proj_dir: Path) -> str:
    """Main app map plus any viewport-specific maps (app.context.<name>.md from
    `web-qa-explore --viewport <name>`) — mobile TCs need the mobile DOM, not guesses."""
    webqa = proj_dir / ".web-qa"
    parts: list[str] = []
    main = webqa / "app.context.md"
    if main.is_file():
        parts.append(main.read_text(encoding="utf-8"))
    for extra in sorted(webqa.glob("app.context.*.md")):
        parts.append(extra.read_text(encoding="utf-8"))
    if not parts:
        return "(no app.context.md — run web-qa-explore first for grounded selectors)"
    # a huge main map must not tail-truncate the viewport maps appended after it
    if len(parts) > 1:
        main_budget = MAX_CONTEXT_CHARS * 2 // 3
        if len(parts[0]) > main_budget:
            parts[0] = parts[0][:main_budget] + "\n…(main map truncated)"
    ctx = "\n\n".join(parts)
    if len(ctx) > MAX_CONTEXT_CHARS:
        ctx = ctx[:MAX_CONTEXT_CHARS] + "\n…(truncated)"
    return ctx


def load_seed(proj_dir: Path) -> str:
    """Optional committed .web-qa/seed.spec.ts — human-verified auth/setup code. Far stronger
    grounding than a prose auth hint: the model reuses working patterns instead of inventing."""
    p = proj_dir / ".web-qa" / "seed.spec.ts"
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8")[:6000]


def seed_prompt_section(seed: str) -> str:
    """Shared prompt block for the seed spec (spec_gen + maintain edit it in one place)."""
    if not seed:
        return ""
    return ("\nKNOWN-GOOD SEED SPEC (human-verified code from THIS repo — reuse its auth/setup "
            "patterns VERBATIM instead of inventing your own):\n```ts\n" + seed + "\n```\n")


def gen_one(tc_key: str, prompt: str, out_path: Path, webqa: Path,
            proj: dict | None = None, live_probe: bool = True,
            role: str | None = None) -> tuple[str, str | None, str | None]:
    """Generate + validate one spec. Returns (tc_key, error_or_None, probe_warning_or_None).

    Acceptance ladder: parse check (`--list`) is a hard gate with retry; the live locator
    probe earns ONE extra retry with real-DOM feedback, but never blocks acceptance — a
    probed miss can legitimately be a mid-flow element the entry page doesn't have.
    `role` = the TC's declared role: the probe must see the page under the SAME session
    the spec will use, or role-gated elements produce false verdicts."""
    from locator_probe import probe_feedback, probe_spec
    attempt_prompt = prompt
    last_err: str | None = None
    probe_retried = False
    for _ in range(GEN_ATTEMPTS):
        code = call_claude(attempt_prompt)
        if not code.strip():
            last_err = "empty output from claude"
            continue
        out_path.write_text(postprocess_spec(code), encoding="utf-8")
        parse_err = validate_spec(webqa, out_path)
        if parse_err is not None:
            last_err = f"playwright --list rejected spec: {parse_err}"
            attempt_prompt = prompt + RETRY_SUFFIX.format(error=parse_err)
            continue
        fb = None
        if proj is not None and live_probe:
            fb = probe_feedback(probe_spec(out_path, proj, role))
        if fb and not probe_retried:
            probe_retried = True
            attempt_prompt = prompt + PROBE_RETRY_SUFFIX.format(feedback=fb)
            continue
        return tc_key, None, fb
    return tc_key, last_err, None


def gen_specs(alias: str, *, all_tcs: bool = False, only_tc: str | None = None,
              force: bool = False, workers: int = 3, live_probe: bool = True) -> dict:
    proj = load_project(alias)
    proj_dir = Path(proj["path"])
    webqa = proj_dir / ".web-qa"
    scenarios_dir = webqa / "scenarios"
    specs_dir = webqa / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    cache_path = specs_dir / ".cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    frontend_url = proj.get("target_url") or proj.get("frontend_url") or "http://127.0.0.1:3000"
    backend_url = proj.get("backend_url") or frontend_url
    stack = proj.get("stack") or "web"
    test_data_prefix = proj.get("test_data_prefix") or "QA-"
    auth_login_hint = proj.get("auth_login_hint") or DEFAULT_AUTH_HINT
    login_email, login_password = resolve_credentials(proj, None, None)
    app_context = load_app_context(proj_dir)
    seed = load_seed(proj_dir)
    seed_section = seed_prompt_section(seed)
    frontend_dir = proj_dir / proj["frontend_dir"] if proj.get("frontend_dir") else proj_dir
    dnd_section = dnd_recipe_section(detect_dnd_library(frontend_dir))
    # Cache key covers everything that shapes the output: TC body + template + app map + seed + urls
    env_hash = tc_hash(PROMPT_TEMPLATE + app_context + seed + frontend_url + backend_url + dnd_section)

    md_files = sorted(scenarios_dir.glob("*.md"))
    if not md_files:
        return {"error": f"no scenarios in {scenarios_dir}"}

    # config `live_probe: false` disables the live locator check (e.g. CI without a stand)
    live_probe = live_probe and proj.get("live_probe") is not False
    summary = {"generated": [], "skipped_cached": [], "skipped_passive": [], "errors": [],
               "probe_warnings": []}
    jobs: list[tuple[str, str, Path, str, str | None]] = []  # (tc_key, prompt, out_path, body_hash, role)

    for md in md_files:
        scenario_stem = md.stem
        tcs = split_tcs(md.read_text(encoding="utf-8"))
        for tc in tcs:
            tc_id = tc["id"]
            tc_key = f"{scenario_stem}::{tc_id}"
            if only_tc and only_tc != tc_id:
                continue
            if not all_tcs and not is_mutating(tc):
                summary["skipped_passive"].append(tc_key)
                continue
            declared = tc_roles(tc.get("body", ""))
            if declared:
                try:
                    tc_email, tc_password = resolve_credentials(proj, None, None, role=declared[0])
                except SystemExit:
                    summary["errors"].append({
                        "tc": tc_key,
                        "error": f"TC declares role {declared[0]!r} but projects.json has no such role",
                    })
                    continue
            else:
                tc_email, tc_password = login_email, login_password
            body_hash = tc_hash(tc.get("body", "") + env_hash + tc_email + tc_password)
            if not force and cache.get(tc_key) == body_hash:
                summary["skipped_cached"].append(tc_key)
                continue
            slug = slugify(tc_id + "-" + tc.get("title", ""))
            out_path = specs_dir / f"{scenario_stem}__{slug}.spec.ts"
            prompt = PROMPT_TEMPLATE.format(
                stack=stack,
                frontend_url=frontend_url,
                backend_url=backend_url,
                login_email=tc_email,
                login_password=tc_password,
                test_data_prefix=test_data_prefix,
                auth_login_hint=auth_login_hint,
                seed_section=seed_section,
                app_context=app_context,
                dnd_section=dnd_section,
                tc_body=f"## {tc_id} — {tc.get('name', '')}\n\n{tc.get('body', '')}",
            )
            jobs.append((tc_key, prompt, out_path, body_hash, declared[0] if declared else None))

    if jobs:
        print(f"[gen] {len(jobs)} spec(s) to generate, {workers} workers", flush=True)
        hash_by_key = {k: h for k, _, _, h, _ in jobs}
        path_by_key = {k: o for k, _, o, _, _ in jobs}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(gen_one, k, p, o, webqa, proj, live_probe, role): k
                       for k, p, o, _, role in jobs}
            for fut in as_completed(futures):
                tc_key = futures[fut]
                out_path = path_by_key[tc_key]
                marker = out_path.with_name(out_path.name + ".FAILED")
                warning = None
                try:
                    _, err, warning = fut.result()
                except Exception as e:
                    err = str(e)
                if warning:
                    summary["probe_warnings"].append({"tc": tc_key, "warning": warning})
                    print(f"[gen] PROBE {tc_key}: unresolved locators remain\n{warning}",
                          file=sys.stderr, flush=True)
                if err is None:
                    cache[tc_key] = hash_by_key[tc_key]
                    summary["generated"].append(tc_key)
                    marker.unlink(missing_ok=True)
                    print(f"[gen] OK   {tc_key}", flush=True)
                else:
                    summary["errors"].append({"tc": tc_key, "error": err})
                    # durable loud marker in specs/ — a silently missing spec is invisible,
                    # a *.FAILED file next to its siblings is not
                    marker.write_text(f"{tc_key}\n{err}\n", encoding="utf-8")
                    print(f"[gen] FAIL {tc_key}: {err[:200]}", file=sys.stderr, flush=True)

    cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    ap.add_argument("--all", action="store_true", help="generate all TCs, not only mutating")
    ap.add_argument("--tc", help="only generate this TC id (e.g. TC-I4)")
    ap.add_argument("--force", action="store_true", help="ignore cache")
    ap.add_argument("--workers", type=int, default=3, help="parallel claude calls (default 3)")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the live locator check against the running app")
    args = ap.parse_args()

    res = gen_specs(args.alias, all_tcs=args.all, only_tc=args.tc, force=args.force,
                    workers=args.workers, live_probe=not args.no_probe)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if not res.get("errors") else 1


if __name__ == "__main__":
    sys.exit(main())
