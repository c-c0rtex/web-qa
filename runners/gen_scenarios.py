"""Phase 3 — generate scenario markdown from a git diff or a task description.

Maps the change/task onto the crawled app map and asks claude for TestDino-style
test cases in the exact md format the runner parses (## TC-X — title).

Usage:
  gen_scenarios.py --alias my-app --diff main            # TCs for changes vs main
  gen_scenarios.py --alias my-app --task "date filter on the orders page"
  gen_scenarios.py --alias my-app --diff HEAD~3 --out orders-dates.md

Output: <project>/.web-qa/scenarios/<name>.md (refuses to overwrite without --force)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from coverage import control_coverage, control_gap_section
from explore import load_project
from matrix import routes_from_context
from run_scenarios import classify, declared_type, split_tcs, tc_routes
from spec_gen import apply_project_budget, call_claude, llm_spend, load_app_context, slugify

# Deciding WHAT to test is judgment work, and it runs exactly once per invocation —
# unlike spec-gen, which fans a mechanical translation out over every test case. The
# tier is worth the few cents here; WEBQA_CLAUDE_MODEL still overrides it.
SCENARIO_MODEL = "opus"
SCENARIO_EFFORT = "high"

MAX_DIFF_CHARS = 9000

# Content signal: precise access-control tokens only. Bare `role`/`scope` are deliberately
# absent — they false-positive on ARIA `role="dialog"` and `<th scope="col">` markup.
RE_RBAC_CONTENT = re.compile(
    r"hasPermission|require_scope|\bis_admin\b|\bcan_(?:edit|delete|create|view|manage)\b"
    r"|\bpermissions?\b|\brbac\b|\bacl\b",
    re.IGNORECASE,
)
# Filename signal (git --stat lines look like " app/backend/roles.py | 10 ++--"):
# a path containing auth/role/permission is a near-certain access-control change.
RE_RBAC_FILES = re.compile(
    r"^\s*[\w/.-]*(?:auth|role|permission|perm|acl|rbac)[\w/.-]*\s*\|",
    re.IGNORECASE | re.MULTILINE,
)

RBAC_DIRECTIVE = """
RBAC DIRECTIVE — this change LOOKS like it touches permissions/roles. First verify by reading
the diff: if the matches are incidental (ARIA `role=` attributes, CSS, `<th scope>` markup),
IGNORE this directive and test the change normally. If it really is access control:
For EVERY affected role generate a PAIR:
- allowed-path TC: the role sees the control and completes the action THROUGH THE UI
- denied-path TC: the control is absent/disabled in the UI AND direct access (URL open or
  API call) is rejected (403 / redirect) — assert BOTH
Annotate each with `**Role:** <name>`. Cover every role listed above, not just one.
If the project has only ONE role, the denied-path is the UNAUTHENTICATED visitor instead:
direct URL → redirect to login, API call → 401 (no `**Role:**` annotation needed).
"""

PROMPT = """You are a senior QA engineer writing test-case scenarios for a web app.

APP MAP (auto-crawled; REAL routes, forms, buttons, tables — target only what exists here):
{app_context}
{coverage_section}
{roles_section}
{source_section}

TASK: Write 3-8 focused test cases covering the change/task above. Golden path first, then
edge cases. Only cover behaviour reachable through the app map routes.

OUTPUT FORMAT (STRICT — this file is parsed by regex, follow it exactly):
# <Scenario title, one line>

## TC-{prefix}1 — <short imperative title>
**Type:** passive|mutating
**Role:** <role name — ONLY for role-specific TCs; omit for role-agnostic ones>
**Steps:**
1. <step>
2. <step>
**Expected:**
- <observable outcome>
- <observable outcome>

## TC-{prefix}2 — <...>
...

RULES:
- `**Type:**` is REQUIRED on every TC: `passive` = read-only checks, `mutating` = creates/edits/deletes
  data. A TC without it is treated as mutating (runners never guess intent from prose)
- Frontend paths in backticks: `/orders`. Backend calls as: GET `/orders/facets`
- Expected bullets must be OBSERVABLE on the page (visible text, table columns, counters)
- CORRECTNESS, not presence. Any TC whose page displays data the app DERIVED (a KPI, a total,
  a count, a ranking, a currency sum) MUST have at least one Expected bullet asserting that
  the displayed value is RIGHT, and naming where that truth comes from. "KPI tiles show
  numbers" is worthless — a broken aggregate happily shows `0`
- Prefer a truth the app itself can be made to state twice. In order:
  1. DRILL-DOWN: "clicking the «Overdue» tile opens the invoice list filtered to overdue, and
     its row count equals the number the tile showed" — nothing is recomputed, so nothing is
     invented
  2. DELTA: "after archiving one overdue invoice through the UI, the tile drops by exactly 1"
  3. RE-COMPUTATION, and only when the metric's definition is written down somewhere you can
     cite: "the «Overdue» tile equals the number of INVOICES (not orders) whose
     `status_payment` is the wire value `overdue`, per GET `/invoices`". State the UNIT and use
     WIRE values, never the label the UI renders for them. Getting the unit wrong makes the
     test red while the app is right
- Never take the truth from the same aggregate/summary endpoint the page itself calls — if
  that endpoint's aggregation is broken, the check passes on a broken feature. If no
  independent source exists, say so in the bullet and assert an invariant instead (ordering,
  sum of parts equals the displayed total)
- If behaviour differs per role, write SEPARATE TCs annotated `**Role:** <name>` — never mix
  two roles' expectations in one TC. Unannotated TCs run under the default account
- Write steps/expected in {language}; keep ids/paths/technical terms as-is
- Output ONLY the markdown, no commentary before or after

Write the scenario now:
"""


def route_coverage(webqa: Path) -> tuple[list[str], list[str]]:
    """(covered, uncovered) app-map routes, judged against the TCs already on disk.

    Deterministic and zero-token. A model answering one scoped task cannot know what
    earlier runs covered, so without this an entire route — a dashboard, an import page —
    stays untested forever and nobody notices."""
    routes = routes_from_context(webqa)
    if not routes:
        return [], []
    touched: set[str] = set()
    for md in sorted((webqa / "scenarios").glob("*.md")):
        for tc in split_tcs(md.read_text(encoding="utf-8")):
            touched |= tc_routes(tc["body"])
    covered = [r for r in routes if r in touched]
    return covered, [r for r in routes if r not in touched]


def coverage_prompt_section(covered: list[str], uncovered: list[str]) -> str:
    """Tell the model what already exists, so it neither duplicates nor re-misses."""
    if not covered and not uncovered:
        return ""
    lines = ["\nEXISTING COVERAGE (deterministic, computed from scenarios already on disk):"]
    if covered:
        lines.append("- routes with test cases: " + ", ".join(f"`{r}`" for r in covered))
    if uncovered:
        lines.append("- routes with NO test case at all: " + ", ".join(f"`{r}`" for r in uncovered))
        lines.append("If the task above touches any uncovered route, cover it — those are the "
                     "real blind spots. Do not re-test the basics of an already-covered route.")
    return "\n".join(lines)


def git_diff_summary(project_path: Path, ref: str) -> str:
    def run(*args: str) -> str:
        proc = subprocess.run(["git", "-C", str(project_path), *args],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise SystemExit(f"git {' '.join(args)} failed: {proc.stderr[:300]}")
        return proc.stdout

    stat = run("diff", "--stat", ref)
    diff = run("diff", ref)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n…(diff truncated)"
    return (
        f"CHANGE UNDER TEST — git diff vs `{ref}`:\n\n"
        f"Files changed:\n{stat}\n\nDiff:\n```\n{diff}\n```"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--diff", help="git ref to diff against (e.g. main, HEAD~3)")
    src.add_argument("--task", help="free-text feature/task description")
    src.add_argument("--cover-gaps", action="store_true",
                     help="target the app-map routes no existing test case touches")
    ap.add_argument("--out", help="output file name inside scenarios/ (default: derived)")
    ap.add_argument("--prefix", default="G", help="TC id prefix letter(s), default G")
    ap.add_argument("--force", action="store_true", help="overwrite existing scenario file")
    args = ap.parse_args()

    proj = load_project(args.alias)
    apply_project_budget(proj)
    proj_dir = Path(proj["path"])
    webqa = proj_dir / ".web-qa"
    scenarios_dir = webqa / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)

    covered, uncovered = route_coverage(webqa)

    if args.cover_gaps:
        # Two kinds of blind spot: a route nobody visits, and a route everybody visits whose
        # mechanics nobody exercises. The second one hid a whole kanban behind a single test
        # case that read a table, and route coverage called it green.
        gaps = control_coverage(webqa)
        if not uncovered and not gaps:
            print(json.dumps({"error": "no uncovered routes and no untouched controls",
                              "covered": covered}), file=sys.stderr)
            return 1
        wants = []
        if uncovered:
            wants.append("the application routes that currently have no test case at all: "
                         + ", ".join(uncovered))
        if gaps:
            worst = sorted(gaps.items(), key=lambda kv: -len(kv[1]))[:4]
            wants.append("the interactive controls that no test case names, listed per route "
                         "under UNTOUCHED CONTROLS below — start with "
                         + ", ".join(f"{r} ({len(n)})" for r, n in worst))
        args.task = "Cover " + "; and ".join(wants)
        print(f"[generate] --cover-gaps targeting {len(uncovered)} route(s) and "
              f"{sum(len(n) for n in gaps.values())} control(s) on {len(gaps)} route(s)",
              file=sys.stderr)

    if args.diff:
        source_section = git_diff_summary(proj_dir, args.diff)
        default_name = f"diff-{slugify(args.diff)}.md"
    else:
        source_section = f"CHANGE UNDER TEST — task description:\n\n{args.task}"
        default_name = "coverage-gaps.md" if args.cover_gaps else f"{slugify(args.task)}.md"

    role_names = [r.get("name") for r in proj.get("roles") or [] if r.get("name")]
    roles_section = (f"\nPROJECT ROLES (accounts exist for each): {', '.join(role_names)}\n"
                     if role_names else "")
    if role_names and (RE_RBAC_FILES.search(source_section) or RE_RBAC_CONTENT.search(source_section)):
        source_section += RBAC_DIRECTIVE

    out_path = scenarios_dir / (args.out or default_name)
    if out_path.exists() and not args.force:
        print(json.dumps({"error": f"{out_path} exists; use --force or --out"}), file=sys.stderr)
        return 2

    # No ARIA: this prompt writes prose test cases, not locators. Whole-page snapshots would
    # eat the budget and head-truncate `Backend endpoints` and `Enum values` — the very
    # sections the CORRECTNESS rule tells it to cite.
    # Route coverage is a weak proxy: one TC that reads a table marks a route with a kanban,
    # a board/tree toggle and an XLSX export as covered. Name the untouched mechanics too.
    control_gaps = control_coverage(webqa)
    prompt = PROMPT.format(app_context=load_app_context(proj_dir, include_aria=False),
                           coverage_section=coverage_prompt_section(covered, uncovered)
                                            + control_gap_section(control_gaps),
                           roles_section=roles_section,
                           source_section=source_section, prefix=args.prefix,
                           language=proj.get("language") or "English")
    print(f"[generate] asking claude ({'diff ' + args.diff if args.diff else 'task'})…", file=sys.stderr)
    md = call_claude(prompt, timeout=240, model=SCENARIO_MODEL, effort=SCENARIO_EFFORT)
    if not md.strip():
        print(json.dumps({"error": "empty output from claude"}), file=sys.stderr)
        return 1

    tc_ids = re.findall(r"^##\s+(TC-[A-Za-z0-9-]+)", md, re.MULTILINE)
    if not tc_ids:
        print(json.dumps({"error": "output has no '## TC-…' headers, not saving",
                          "head": md[:300]}), file=sys.stderr)
        return 1

    out_path.write_text(md if md.endswith("\n") else md + "\n", encoding="utf-8")

    # Catch a mislabeled Type at birth, while the human is still reviewing the md plan:
    # declared passive + a mutating HTTP op in Steps = contradiction (classify will
    # override it to mutating at run time, but the author should fix the TC now)
    conflicts = []
    for tc in split_tcs(md):
        kind, reasons = classify(tc["body"])
        if declared_type(tc["body"]) == "passive" and kind == "mutating":
            conflicts.append({"id": tc["id"], "reasons": reasons})
            print(f"[generate] WARNING {tc['id']}: {'; '.join(reasons)}", file=sys.stderr)

    summary = {"out": str(out_path), "tc_count": len(tc_ids), "tc_ids": tc_ids}
    if conflicts:
        summary["type_conflicts"] = conflicts

    # Recomputed AFTER the write: a scoped task legitimately leaves gaps, but they must be
    # stated out loud. Silence here is how a whole route stays untested for months.
    _, still_uncovered = route_coverage(webqa)
    summary["uncovered_routes"] = still_uncovered
    if still_uncovered:
        print(f"[generate] COVERAGE GAP — no test case touches: {', '.join(still_uncovered)}"
              f"\n[generate] run `web-qa-generate --alias {args.alias} --cover-gaps` to close them",
              file=sys.stderr)

    still_gaps = control_coverage(webqa)
    if still_gaps:
        summary["uncovered_controls"] = still_gaps
        worst = sorted(still_gaps.items(), key=lambda kv: -len(kv[1]))[:3]
        detail = "; ".join(f"`{r}` ({len(n)})" for r, n in worst)
        total = sum(len(n) for n in still_gaps.values())
        print(f"[generate] CONTROL GAP — {total} control(s) on {len(still_gaps)} route(s) are "
              f"named by no test case; worst: {detail}", file=sys.stderr)

    summary["llm"] = llm_spend()
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
