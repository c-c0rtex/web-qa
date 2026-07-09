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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from explore import load_project, resolve_credentials
from run_scenarios import (DEFAULT_BACKEND_PREFIXES, declared_type, extract_paths, norm_route,
                           split_tcs, tc_roles, tc_routes)


# The map now stores whole ARIA snapshots (explore stopped rationing them), and slice_aria
# keeps only the routes of the TC at hand — usually one or two. A real page snapshot runs
# 10–14 KB, so the old 24 KB ceiling would have truncated the very thing we widened.
MAX_CONTEXT_CHARS = 48000
ARIA_SLICE_BUDGET = 26000   # shared across the snapshots this TC actually visits
ARIA_SLICE_MIN = 6000       # …but never rationed below a usable page
DEFAULT_TEST_TIMEOUT_MS = 60_000
GEN_ATTEMPTS = 2  # initial + one retry with playwright parse error fed back

# --- LLM spend controls ----------------------------------------------------
# `claude -p` is a full headless Claude Code session, not one API call: it ships a
# system prompt, every tool schema, and — unless muzzled — an agentic loop that will
# happily crawl the repo for minutes. Fanning ~30 TCs out across that, on whatever
# frontier model the user's interactive CLI happens to be pinned to, drains a
# subscription window in minutes. These defaults keep generation cheap and bounded.
# Every one is overridable by env var; none of them silently costs more than stated.
DEFAULT_CLAUDE_MODEL = "sonnet"    # spec-gen is a mechanical translation, not frontier reasoning
DEFAULT_CLAUDE_EFFORT = "medium"   # frontier defaults are `high`, which we do not need here
DEFAULT_CLAUDE_TOOLS = ""          # "" = no tools: app.context.md is already in the prompt
DEFAULT_MAX_USD = 5.0              # hard per-process ceiling; 0 disables the guard

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
- Playwright TEST TIMEOUT for this project: {test_timeout_ms} ms (whole test, all steps)
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
- Add `expect(...)` assertions covering the Expected bullets. A presence assertion
  (`toBeVisible`, `toHaveCount`) does NOT cover a bullet that names a value, a number, a
  count or a piece of text — assert the VALUE itself (`toHaveText`, `toContainText`,
  `toHaveValue`, or parse the number and compare it). "The tile renders" is not a test
  that the tile is right
- Selector priority: 1) `getByTestId` when the APP MAP shows a data-testid for the element,
  2) `getByRole` with the accessible name (the ARIA snapshots in the APP MAP are ground truth
  for role/name), 3) `getByLabel` / `getByText`. NEVER CSS classes, XPath or positional nth()
- Selector texts/labels MUST come from the APP MAP above when the route is listed there — do not invent button captions
- Take the ROLE from the snapshot too, not from what the widget looks like. A picker rendered
  as `combobox` is not `getByRole('button')`, and the visible caption inside it is a child
  node, not the element's accessible name
- If the snapshot shows an element with NO accessible name (a bare `- textbox` under a
  separate `- text: Title` node, `- button` with no quoted caption), then `getByLabel(...)`
  and `getByRole(..., {{ name }})` CANNOT match it — the app has no label association there.
  Locate it structurally instead (`getByRole('dialog').getByRole('textbox').nth(i)`, or
  `locator('input').near(page.getByText('Title'))`) and add a `// NOTE: unlabeled input` comment
- When a name occurs more than once in the snapshot, scope before matching
  (`page.getByRole('navigation').getByRole('link', {{ name: 'X' }})`) — an unscoped locator
  that resolves to 2+ elements fails on strict mode, not on the app being wrong
- For navigation ALWAYS: `await page.goto('{frontend_url}<path>', {{ waitUntil: 'domcontentloaded' }})` —
  never the default 'load' and never 'networkidle': Next.js dev keeps an HMR websocket open, so
  those states never settle and the test burns its whole timeout
- NEVER use `waitForLoadState('networkidle')` anywhere. Web-first assertions
  (`await expect(locator).toBeVisible()`) auto-wait and are the correct sync point; for
  navigation waits use `page.waitForURL(...)`
- Mock EXTERNAL third-party dependencies only (payments, outside APIs). NEVER mock or stub
  your own app's backend — the test must exercise the real stack
- Do not hardcode IDs — use `?` if TC is generic about which entity, or pick a plausible id
- Keep the spec self-contained; no external helpers

API CONTRACT RULE (the APP MAP states the contract — never guess it):
- The `Backend endpoints` section gives, per operation, the SUCCESS STATUS CODE and the
  RESPONSE SHAPE. Assert the code it declares (a POST that declares `201` must not be
  asserted `toBe(200)`), and unwrap the shape it declares. If the shape is
  `{{items:array, total:integer}}`, then `(await r.json()).filter(...)` is a crash, not a test
- Comparing a wire enum (`Enum values` section) against a label the UI renders for it is
  wrong even when both sides are right. Compare wire values to wire values, UI text to UI text

DATA CORRECTNESS RULE (a page that renders is not a page that is right):
- When an Expected bullet names a value the app DERIVES from data — a KPI, a total, a count,
  a ranking, a currency sum — assert the VALUE, not its presence. Pick the strongest oracle
  the API actually supports, in this order:
  1. DRILL-DOWN / ROUND-TRIP (preferred): the app itself already offers the detailed view
     behind the summary. Click the tile, open the filtered list, and assert the summary
     equals what the detail view shows. Both come from the app, so no definition is invented
  2. MUTATION INVARIANT: read the value, perform a UI action with a known effect, read it
     again, assert the DELTA. A wrong absolute value cannot fake a correct delta
  3. RE-IMPLEMENTATION (last resort): recompute the value from primary collections. Allowed
     ONLY if the APP MAP or the TC states the metric's definition, and then you MUST write
     that definition in a comment above the computation, including its UNIT (rows? entities?
     currency? which currency?). A re-implemented oracle is a SECOND UNVERIFIED
     IMPLEMENTATION: if you count invoices where the app counts orders, the test is red and
     the app is right
- NEVER derive the expected value from the SAME aggregate/summary endpoint the page itself
  calls. That endpoint is part of what is under test: if its aggregation is wrong, the UI and
  your oracle are wrong together and the test passes on a broken feature
- Zero, empty and "—" are values a broken aggregate loves to return. Asserting "a number is
  rendered" or "no NaN appears" catches none of it. But "—" is also how a UI renders "nothing
  here" — do not assert it equals `''`
- If none of the three oracles is available, say so in a comment and assert the strongest
  invariant you can (ordering, non-negativity, sum of the parts equals the displayed total,
  row count equals a displayed counter) — never fall back to `toBeVisible`

UI-FIRST RULE (what makes the spec worth anything):
- Every user-visible step of the TC MUST be performed through the UI — real clicks on real
  buttons, real form fills, real navigation — exactly as a user would do it
- Doing the action under test via `page.request` instead of the UI is a SPEC BUG: the test
  goes green while the actual user path may be broken
- `page.request` / API calls are allowed ONLY for: authentication, creating/deleting test
  data (setup/teardown), side-verification of state AFTER a UI action, and reading an
  INDEPENDENT ORACLE to check a displayed value against (see DATA CORRECTNESS RULE)
- OPEN THE PAGE FIRST, compute the oracle SECOND. A spec that queries the API before its
  first `page.goto` and then fails leaves playwright screenshotting `about:blank` — the one
  artifact a human needs to judge the failure is blank

AUTH RULE (the single most common way these specs die):
- Use the credentials given above VERBATIM. Never invent an email, never pattern-match one
  from another account's domain
- `page.request` shares the BROWSER context: it carries session cookies, and nothing else.
  If the AUTH FLOW returns a bearer token, every call to {backend_url} must go through a
  request context that sends it — `request.newContext({{ extraHTTPHeaders: {{ Authorization:
  `Bearer ${{token}}` }} }})`. `page.request.get(API_URL)` without that header is a 401
- The only legitimate use of `page.request` against {backend_url} is the login POST itself

TIMING BUDGET (the test has {test_timeout_ms} ms in total):
- Every explicit `timeout:` you write must be well under {test_timeout_ms} ms. A
  `waitFor({{ timeout: 60000 }})` inside a 30 s test cannot ever fire: playwright kills the
  test first and reports a bare "Test timeout exceeded" naming nothing
- Do not add explicit timeouts at all unless a step is genuinely slow (upload, async parse).
  The project already bounds actions and navigations; web-first assertions auto-wait
- NEVER use `page.waitForTimeout()` — assert on the condition you are actually waiting for

FIXTURE RULE:
- Never reference a file path that you have not created in the test. To upload, build the
  file in memory: `setInputFiles({{ name: 'x.pdf', mimeType: 'application/pdf',
  buffer: Buffer.from(...) }})`. A path like `fixtures/sample.pdf` that nobody generates is
  an ENOENT, not a test

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


class LLMBudgetExceeded(RuntimeError):
    """Raised INSTEAD of spending past the ceiling. Nothing reaches the model."""


_ledger_lock = threading.Lock()
_spent_usd = 0.0
_call_count = 0


def llm_budget_usd() -> float:
    """Hard ceiling in USD for this process. <= 0 disables the guard."""
    try:
        return float(os.environ.get("WEBQA_MAX_USD", DEFAULT_MAX_USD))
    except ValueError:
        return DEFAULT_MAX_USD


def llm_spend() -> dict:
    """Ledger snapshot — attached to every runner's summary JSON so a run's cost is
    reported, not guessed."""
    with _ledger_lock:
        return {"spent_usd": round(_spent_usd, 4), "calls": _call_count,
                "budget_usd": llm_budget_usd()}


def _check_budget() -> None:
    budget = llm_budget_usd()
    if budget <= 0:
        return
    with _ledger_lock:
        if _spent_usd >= budget:
            raise LLMBudgetExceeded(
                f"LLM budget exhausted: ${_spent_usd:.2f} spent over {_call_count} call(s), "
                f"ceiling ${budget:.2f}. Nothing was sent to the model. Raise WEBQA_MAX_USD "
                f"(or --max-usd) to continue, or 0 to disable the guard."
            )


def projected_total(total_jobs: int, done_jobs: int) -> float | None:
    """Extrapolate the run's final cost from what has been spent so far.
    None until at least one job finished — an average over zero jobs says nothing."""
    if done_jobs <= 0 or total_jobs <= 0:
        return None
    spent = llm_spend()["spent_usd"]
    return spent / done_jobs * total_jobs


def _record_spend(usd: float) -> float:
    global _spent_usd, _call_count
    with _ledger_lock:
        _spent_usd += usd
        _call_count += 1
        return _spent_usd


def strip_fences(out: str) -> str:
    """Drop code fences the model added despite instructions."""
    out = re.sub(r"^```(?:typescript|ts)?\s*\n?", "", out.strip())
    return re.sub(r"\n?```\s*$", "", out)


def _resolve(env_var: str, override: str | None, default: str) -> str:
    """Precedence: env var (user's explicit escape hatch) > caller's tier > default."""
    from_env = os.environ.get(env_var)
    if from_env is not None:
        return from_env
    return override if override is not None else default


def claude_cmd(prompt: str, *, model: str | None = None, effort: str | None = None,
               tools: str | None = None) -> list[str]:
    """Argv for one metered, tool-less `claude -p` session. Separated from call_claude
    so the cost controls are testable without spawning the CLI.

    Callers name their own tier: deciding WHAT to test is judgment work worth a strong
    model (once per run), turning a decided test case into code is translation."""
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--strict-mcp-config"]
    m = _resolve("WEBQA_CLAUDE_MODEL", model, DEFAULT_CLAUDE_MODEL)
    if m:
        cmd += ["--model", m]
    e = _resolve("WEBQA_CLAUDE_EFFORT", effort, DEFAULT_CLAUDE_EFFORT)
    if e:
        cmd += ["--effort", e]
    cmd += ["--tools", _resolve("WEBQA_CLAUDE_TOOLS", tools, DEFAULT_CLAUDE_TOOLS)]
    return cmd


def parse_claude_json(stdout: str) -> str:
    """Pull the text result out of `--output-format json` and meter the spend.
    Falls back to raw stdout if the CLI ever stops emitting JSON."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return strip_fences(stdout)
    if not isinstance(payload, dict):
        return strip_fences(stdout)
    if payload.get("is_error"):
        raise RuntimeError(f"claude session errored: {str(payload.get('result'))[:500]}")
    cost = payload.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        total = _record_spend(float(cost))
        print(f"[llm] ${float(cost):.4f} this call, ${total:.2f} of "
              f"${llm_budget_usd():.2f} budget ({payload.get('num_turns', '?')} turn(s))",
              file=sys.stderr, flush=True)
    return strip_fences(str(payload.get("result") or ""))


def call_claude(prompt: str, timeout: int | None = None, *, model: str | None = None,
                effort: str | None = None, tools: str | None = None) -> str:
    """Run one headless `claude -p` session and return its text result.

    This is the ONLY place web-qa spends tokens — spec_gen, gen_scenarios and maintain
    all funnel through it — so the cost controls live here rather than at each caller:

      * tools are OFF by default: the prompt already carries app.context.md, so an
        agentic loop re-reading the repo buys nothing and costs a great deal;
      * the model defaults to `sonnet` rather than inheriting whatever the user's
        interactive CLI is pinned to (a frontier default is several times the price
        for what is, at this call site, a mechanical translation). Callers that do
        judgment work rather than translation pass a stronger `model=`;
      * spend is metered via `--output-format json` and capped by WEBQA_MAX_USD.

    Env overrides (they beat the caller's tier): WEBQA_CLAUDE_MODEL, WEBQA_CLAUDE_EFFORT,
    WEBQA_CLAUDE_TOOLS, WEBQA_MAX_USD, WEBQA_GEN_TIMEOUT (seconds, default 300 — complex
    multi-step TCs did not fit the old 180s and died silently).
    """
    _check_budget()
    timeout = timeout or int(os.environ.get("WEBQA_GEN_TIMEOUT", "300"))
    cmd = claude_cmd(prompt, model=model, effort=effort, tools=tools)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        # The CLI reports usage-limit exhaustion on stdout, not stderr. Reading only
        # stderr turned "5-hour limit reached" into a blank, unactionable error.
        detail = (proc.stderr.strip() or proc.stdout.strip() or "(no output)")[:500]
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {detail}")
    return parse_claude_json(proc.stdout)


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


MANUAL_MARKER = "<!-- manual -->"
RE_ARIA_ENTRY = re.compile(r"### `([^`]+)`\n```yaml\n.*?\n```", re.S)


def _route_match(page_path: str, routes: set[str]) -> bool:
    """A snapshot is relevant if the TC visits that page, one of its children (a row click
    lands on `/orders/{id}` even when Steps only name `/orders`), or its parent. `/` is
    nobody's prefix — it would otherwise match the whole map."""
    n = norm_route(page_path)
    for r in routes:
        if n == r:
            return True
        if r != "/" and n.startswith(r.rstrip("/") + "/"):
            return True
        if n != "/" and r.startswith(n.rstrip("/") + "/"):
            return True
    return False


def slice_aria(md: str, routes: set[str]) -> str:
    """Keep only the ARIA snapshots for routes this TC visits.

    The section is identical for every TC and dwarfs everything else, so a head-truncation
    silently dropped whichever routes rendered last — `/orders` among them. A spec for
    `/orders` never needed the DOM of `/help`. Slicing removes the truncation, raises
    relevance, and cuts the prompt for all N spec-gen calls."""
    if not routes:
        return md
    start = md.find("## ARIA snapshots")
    if start < 0:
        return md
    end = md.find("\n## ", start + 1)
    if end < 0:
        end = len(md)
    header, _, body = md[start:end].partition("\n")
    entries = list(RE_ARIA_ENTRY.finditer(body))
    kept = [m.group(0) for m in entries if _route_match(m.group(1), routes)]
    if not kept:
        return md            # TC touches nothing we mapped — better the whole section than none
    # The prompt budget belongs here, where we know how few snapshots survive the slice —
    # not in explore, where dividing it across every route in the app starved all of them.
    per = max(ARIA_SLICE_MIN, ARIA_SLICE_BUDGET // len(kept))
    kept = [k if len(k) <= per else k[:per] + "\n# …(snapshot clipped)\n```" for k in kept]
    note = (f"_(showing {len(kept)} of {len(entries)} route snapshots — "
            f"the ones this test case visits)_")
    return md[:start] + header + "\n\n" + note + "\n\n" + "\n".join(kept) + "\n" + md[end:]


def _section_bounds(md: str, title: str) -> tuple[int, int] | None:
    s = md.find(title)
    if s < 0:
        return None
    e = md.find("\n## ", s + 1)
    return s, (len(md) if e < 0 else e)


def api_group(path: str) -> str:
    """`/orders/{id}/items` → `/orders` — the heading the OpenAPI section groups under."""
    head = path.strip("/").split("/")[0]
    return f"/{head}" if head else "/"


def tc_api_groups(body: str, backend_prefixes: tuple[str, ...]) -> set[str]:
    """API groups a TC could legitimately need: the pages it visits, the endpoints it names
    outright (the independent-oracle rule makes TCs name their primary collections), and
    auth — every spec logs in."""
    fronts, backs = extract_paths(body, backend_prefixes)
    groups = {api_group(p) for p in fronts} | {api_group(p) for _, p in backs}
    groups.add("/auth")
    return groups


def slice_openapi(md: str, groups: set[str]) -> str:
    """Keep only the endpoint groups this TC can plausibly call. The section can be a third
    of the map and is identical for every TC; a spec for one page never needed the request
    schemas of every other resource in the API."""
    b = _section_bounds(md, "## Backend endpoints")
    if not b or not groups:
        return md
    s, e = b
    header, _, body = md[s:e].partition("\n")
    blocks = [blk for blk in re.split(r"(?m)^(?=### )", body) if blk.strip().startswith("### ")]
    # The enum block is a `###` sibling of the endpoint groups but belongs to no group. It was
    # therefore filtered out of every single prompt — while the prompt told the model to take
    # allowed values from it. Endpoint groups are sliced; the enum block always rides along.
    enums = [blk for blk in blocks if blk.strip().startswith("### Enum values")]
    endpoints = [blk for blk in blocks if blk not in enums]
    kept = [blk for blk in endpoints
            if (m := re.match(r"### `([^`]+)`", blk.strip())) and m.group(1) in groups]
    if not kept:
        return md
    note = (f"_(showing {len(kept)} of {len(endpoints)} endpoint groups — "
            f"the ones this test case can call)_")
    return (md[:s] + header + "\n\n" + note + "\n\n"
            + "".join(kept).rstrip() + "\n\n" + "".join(enums).rstrip() + "\n" + md[e:])


def _protect_manual(md: str, budget: int) -> str:
    """Truncate the auto-generated head, never the hand-written tail below MANUAL_MARKER."""
    if len(md) <= budget:
        return md
    i = md.find(MANUAL_MARKER)
    if i < 0:
        return md[:budget] + "\n…(truncated)"
    auto, manual = md[:i], md[i:]
    head = max(0, budget - len(manual) - 20)
    if head <= 0:                      # a manual section larger than the whole budget: keep it
        return manual
    return auto[:head] + "\n…(auto map truncated)\n\n" + manual


def drop_aria(md: str) -> str:
    """Remove the ARIA section entirely, leaving a pointer.

    Whole-page snapshots make the map hundreds of KB. A caller that needs no selectors — the
    scenario generator writes prose test cases, not locators — would otherwise spend its whole
    budget on the ARIA section and head-truncate away `Backend endpoints` and `Enum values`,
    which are exactly what its oracle rule cites."""
    b = _section_bounds(md, "## ARIA snapshots")
    if not b:
        return md
    s, e = b
    header, _, _ = md[s:e].partition("\n")
    return md[:s] + header + "\n\n_(snapshots omitted — not needed for this task)_\n" + md[e:]


def map_files(proj_dir: Path) -> list[Path]:
    webqa = proj_dir / ".web-qa"
    main = webqa / "app.context.md"
    return ([main] if main.is_file() else []) + sorted(webqa.glob("app.context.*.md"))


def app_map_fingerprint(proj_dir: Path) -> str:
    """Digest of the map as it is ON DISK.

    Keying the spec cache on `load_app_context()` output digested a TRUNCATED map, so a
    re-crawl that changed anything past the truncation point left every cached spec looking
    current."""
    return tc_hash("".join(p.read_text(encoding="utf-8") for p in map_files(proj_dir)))


def load_app_context(proj_dir: Path, routes: set[str] | None = None,
                     api_groups: set[str] | None = None,
                     include_aria: bool = True) -> str:
    """Main app map plus any viewport-specific maps (app.context.<name>.md from
    `web-qa-explore --viewport <name>`) — mobile TCs need the mobile DOM, not guesses.

    `routes` (the routes of the TC being generated) slices the ARIA section down to the
    pages that TC visits. Without it the same 25 KB of snapshots rides along in every call
    and the tail gets truncated away. `include_aria=False` drops the section outright."""
    parts = [p.read_text(encoding="utf-8") for p in map_files(proj_dir)]
    if not parts:
        return "(no app.context.md — run web-qa-explore first for grounded selectors)"
    if not include_aria:
        parts = [drop_aria(p) for p in parts]
    elif routes:
        parts = [slice_aria(p, routes) for p in parts]
    if api_groups:
        parts = [slice_openapi(p, api_groups) for p in parts]
    # The manual section is the ONLY hand-written part of the map ("business rules the
    # crawler can't see"). It lives at the tail, so a head-truncation dropped it entirely —
    # web-qa invited the user to write knowledge there and then never showed it to the model.
    parts = [_protect_manual(p, MAX_CONTEXT_CHARS * 2 // 3 if len(parts) > 1 else MAX_CONTEXT_CHARS)
             for p in parts]
    ctx = "\n\n".join(parts)
    if len(ctx) > MAX_CONTEXT_CHARS:
        ctx = _protect_manual(ctx, MAX_CONTEXT_CHARS)
    return ctx


# `timeout: 30_000` and the template's `timeout: Number(process.env.WEBQA_TEST_TIMEOUT ?? 60_000)`.
# Anchored at line start so `expect: { timeout: 8_000 }` — a different budget — cannot match.
RE_PW_TIMEOUT = re.compile(r"^\s*timeout:\s*(?:Number\([^)]*\?\?\s*)?([0-9_]+)", re.MULTILINE)


def read_test_timeout(webqa: Path) -> int:
    """The project's playwright `timeout:` (whole-test budget), in ms.

    The generator cannot respect a budget it was never told. Left to itself it writes
    `waitFor({timeout: 60000})` into a 30 s test, which playwright kills at 30 s with a
    message that names no locator and no step."""
    env = os.environ.get("WEBQA_TEST_TIMEOUT")
    if env and env.strip().isdigit():
        return int(env.strip())
    cfg = webqa / "playwright.config.ts"
    if not cfg.is_file():
        return DEFAULT_TEST_TIMEOUT_MS
    m = RE_PW_TIMEOUT.search(cfg.read_text(encoding="utf-8"))
    if not m:
        return DEFAULT_TEST_TIMEOUT_MS
    try:
        return int(m.group(1).replace("_", ""))
    except ValueError:
        return DEFAULT_TEST_TIMEOUT_MS


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
              force: bool = False, workers: int = 1, live_probe: bool = True) -> dict:
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
    backend_prefixes = tuple(proj.get("backend_prefixes") or DEFAULT_BACKEND_PREFIXES)
    # Per-TC below (ARIA sliced to the routes that TC visits). The FULL map still keys the
    # cache: a re-crawl that changes any route must invalidate every spec, not just the
    # ones whose own slice moved.
    app_map_digest = app_map_fingerprint(proj_dir)
    seed = load_seed(proj_dir)
    seed_section = seed_prompt_section(seed)
    test_timeout_ms = read_test_timeout(webqa)
    frontend_dir = proj_dir / proj["frontend_dir"] if proj.get("frontend_dir") else proj_dir
    dnd_section = dnd_recipe_section(detect_dnd_library(frontend_dir))
    # Cache key covers everything that shapes the output: TC body + template + app map + seed + urls
    env_hash = tc_hash(PROMPT_TEMPLATE + app_map_digest + seed + frontend_url + backend_url
                       + dnd_section + str(test_timeout_ms))

    md_files = sorted(scenarios_dir.glob("*.md"))
    if not md_files:
        return {"error": f"no scenarios in {scenarios_dir}"}

    # config `live_probe: false` disables the live locator check (e.g. CI without a stand)
    live_probe = live_probe and proj.get("live_probe") is not False
    summary = {"generated": [], "skipped_cached": [], "skipped_passive": [], "errors": [],
               "probe_warnings": [], "skipped_missing_role": [], "skipped_over_budget": []}
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
                    # A registry gap, not a generation failure: costs nothing, fixed by the
                    # human, and must not be buried among real errors or leave a .FAILED marker
                    summary["skipped_missing_role"].append({"tc": tc_key, "role": declared[0]})
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
                test_timeout_ms=test_timeout_ms,
                seed_section=seed_section,
                app_context=load_app_context(proj_dir, tc_routes(tc.get("body", "")),
                                             tc_api_groups(tc.get("body", ""), backend_prefixes)),
                dnd_section=dnd_section,
                tc_body=f"## {tc_id} — {tc.get('name', '')}\n\n{tc.get('body', '')}",
            )
            jobs.append((tc_key, prompt, out_path, body_hash, declared[0] if declared else None))

    if summary["skipped_missing_role"]:
        gaps = ", ".join(f"{r['tc']} (role {r['role']!r})" for r in summary["skipped_missing_role"])
        print(f"[gen] {len(summary['skipped_missing_role'])} TC(s) skipped, no such role in "
              f"projects.json — add `roles: [{{name, email, password}}]`: {gaps}",
              file=sys.stderr, flush=True)

    budget_stop: str | None = None
    if jobs:
        budget = llm_budget_usd()
        cap = f"${budget:.2f} budget" if budget > 0 else "budget guard OFF"
        print(f"[gen] {len(jobs)} spec(s) to generate, {workers} worker(s), {cap}", flush=True)
        hash_by_key = {k: h for k, _, _, h, _ in jobs}
        path_by_key = {k: o for k, _, o, _, _ in jobs}
        projected_warned = False
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
                except LLMBudgetExceeded as e:
                    # Nothing was sent to the model. A .FAILED marker means "the generator
                    # produced a bad spec" — writing one here libels the generator and buries
                    # the real cause under one marker per remaining TC.
                    summary["skipped_over_budget"].append(tc_key)
                    budget_stop = budget_stop or str(e)
                    continue
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

                # Warn while there is still something to decide. Running out of budget on the
                # last TC of twenty is a worse outcome than being told at the third.
                if not projected_warned and budget > 0:
                    done = len(summary["generated"]) + len(summary["errors"])
                    projected = projected_total(len(jobs), done)
                    if done >= 2 and projected is not None and projected > budget:
                        projected_warned = True
                        print(f"[gen] PROJECTION ${projected:.2f} for {len(jobs)} spec(s) exceeds "
                              f"the ${budget:.2f} ceiling — raise --max-usd now or expect a stop",
                              file=sys.stderr, flush=True)

    cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    summary["llm"] = llm_spend()

    if summary["skipped_over_budget"]:
        spent = summary["llm"]["spent_usd"]
        left = summary["skipped_over_budget"]
        print(f"\n[gen] BUDGET STOP — {budget_stop}", file=sys.stderr)
        print(f"[gen] {len(left)} TC(s) never reached the model (no .FAILED written): "
              f"{', '.join(left)}", file=sys.stderr)
        # the cache holds what succeeded, so a resume regenerates only what is missing
        print(f"[gen] resume (keep your other flags): web-qa-spec-gen --alias {alias} "
              f"--max-usd {spent + 0.5 * max(1, len(left)):.2f}", file=sys.stderr, flush=True)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    ap.add_argument("--all", action="store_true", help="generate all TCs, not only mutating")
    ap.add_argument("--tc", help="only generate this TC id (e.g. TC-I4)")
    ap.add_argument("--force", action="store_true", help="ignore cache")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel claude calls (default 1). Concurrent calls all miss the "
                         "shared prompt cache — each extra worker buys wall-clock with money")
    ap.add_argument("--max-usd", type=float, default=None,
                    help=f"hard LLM spend ceiling for this run (default {DEFAULT_MAX_USD:.2f}, "
                         f"0 = no guard). Also settable via WEBQA_MAX_USD")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the live locator check against the running app")
    args = ap.parse_args()
    if args.max_usd is not None:
        os.environ["WEBQA_MAX_USD"] = str(args.max_usd)

    res = gen_specs(args.alias, all_tcs=args.all, only_tc=args.tc, force=args.force,
                    workers=args.workers, live_probe=not args.no_probe)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    # A budget stop leaves work undone, so it is not success — but it is not an error either,
    # and `skipped_missing_role` is a registry gap the human fixes, never a failed run.
    return 0 if not (res.get("errors") or res.get("skipped_over_budget")) else 1


if __name__ == "__main__":
    sys.exit(main())
