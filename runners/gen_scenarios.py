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

from explore import load_project
from matrix import routes_from_context
from run_scenarios import (RE_BACKEND_OP, RE_PATH_BACKTICKED, classify, declared_type,
                           split_tcs)
from spec_gen import call_claude, llm_spend, load_app_context, slugify

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
- If behaviour differs per role, write SEPARATE TCs annotated `**Role:** <name>` — never mix
  two roles' expectations in one TC. Unannotated TCs run under the default account
- Write steps/expected in {language}; keep ids/paths/technical terms as-is
- Output ONLY the markdown, no commentary before or after

Write the scenario now:
"""


RE_STEPS = re.compile(r"\*\*Steps?:\*\*(.*?)(?=\*\*Expected|\Z)", re.S | re.IGNORECASE)


def _norm_route(path: str) -> str:
    """Collapse a TC's concrete path onto the same template shape routes_from_context uses."""
    r = re.sub(r"\{[^}]+\}", "{id}", path.split("?")[0]) or "/"
    return re.sub(r"/\d+(?=/|$)", "/{id}", r)


def tc_routes(body: str) -> set[str]:
    """Routes a test case actually navigates to.

    Steps only, and never the target of an HTTP verb. Both restrictions are load-bearing:
    an Expected bullet reading "redirects to `/`" names a route the TC never exercises,
    and a step that documents its own `GET /admin/users` call still visits that page.
    Using the runner's frontend/backend split here got both cases backwards."""
    m = RE_STEPS.search(body)
    if not m:
        return set()
    steps = RE_BACKEND_OP.sub(" ", m.group(1))   # strip `GET /x` API references
    return {_norm_route(hit.group(1).rstrip(".,;:"))
            for hit in RE_PATH_BACKTICKED.finditer(steps)}


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
    proj_dir = Path(proj["path"])
    webqa = proj_dir / ".web-qa"
    scenarios_dir = webqa / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)

    covered, uncovered = route_coverage(webqa)

    if args.cover_gaps:
        if not uncovered:
            print(json.dumps({"error": "no uncovered routes — nothing to generate",
                              "covered": covered}), file=sys.stderr)
            return 1
        args.task = ("Cover the application routes that currently have no test case at all: "
                     + ", ".join(uncovered))
        print(f"[generate] --cover-gaps targeting {len(uncovered)} route(s): "
              f"{', '.join(uncovered)}", file=sys.stderr)

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

    prompt = PROMPT.format(app_context=load_app_context(proj_dir),
                           coverage_section=coverage_prompt_section(covered, uncovered),
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

    summary["llm"] = llm_spend()
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
