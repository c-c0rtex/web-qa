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
from spec_gen import call_claude, load_app_context, slugify

MAX_DIFF_CHARS = 9000

RE_RBAC_DIFF = re.compile(
    r"permission|\brole\b|\broles\b|\bscope|hasPermission|require_scope|\bacl\b|\brbac\b|can_edit|is_admin",
    re.IGNORECASE,
)

RBAC_DIRECTIVE = """
RBAC DIRECTIVE — the change touches permissions/roles. For EVERY affected role generate a PAIR:
- allowed-path TC: the role sees the control and completes the action THROUGH THE UI
- denied-path TC: the control is absent/disabled in the UI AND direct access (URL open or
  API call) is rejected (403 / redirect) — assert BOTH
Annotate each with `**Role:** <name>`. Cover every role listed above, not just one.
"""

PROMPT = """You are a senior QA engineer writing test-case scenarios for a web app.

APP MAP (auto-crawled; REAL routes, forms, buttons, tables — target only what exists here):
{app_context}
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
- `Type: passive` = read-only checks; `Type: mutating` = creates/edits/deletes data
- Frontend paths in backticks: `/orders`. Backend calls as: GET `/orders/facets`
- Expected bullets must be OBSERVABLE on the page (visible text, table columns, counters)
- If behaviour differs per role, write SEPARATE TCs annotated `**Role:** <name>` — never mix
  two roles' expectations in one TC. Unannotated TCs run under the default account
- Write steps/expected in {language}; keep ids/paths/technical terms as-is
- Output ONLY the markdown, no commentary before or after

Write the scenario now:
"""


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
    ap.add_argument("--out", help="output file name inside scenarios/ (default: derived)")
    ap.add_argument("--prefix", default="G", help="TC id prefix letter(s), default G")
    ap.add_argument("--force", action="store_true", help="overwrite existing scenario file")
    args = ap.parse_args()

    proj = load_project(args.alias)
    proj_dir = Path(proj["path"])
    scenarios_dir = proj_dir / ".web-qa" / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)

    if args.diff:
        source_section = git_diff_summary(proj_dir, args.diff)
        default_name = f"diff-{slugify(args.diff)}.md"
    else:
        source_section = f"CHANGE UNDER TEST — task description:\n\n{args.task}"
        default_name = f"{slugify(args.task)}.md"

    role_names = [r.get("name") for r in proj.get("roles") or [] if r.get("name")]
    roles_section = (f"\nPROJECT ROLES (accounts exist for each): {', '.join(role_names)}\n"
                     if role_names else "")
    if RE_RBAC_DIFF.search(source_section) and role_names:
        source_section += RBAC_DIRECTIVE

    out_path = scenarios_dir / (args.out or default_name)
    if out_path.exists() and not args.force:
        print(json.dumps({"error": f"{out_path} exists; use --force or --out"}), file=sys.stderr)
        return 2

    prompt = PROMPT.format(app_context=load_app_context(proj_dir),
                           roles_section=roles_section,
                           source_section=source_section, prefix=args.prefix,
                           language=proj.get("language") or "English")
    print(f"[generate] asking claude ({'diff ' + args.diff if args.diff else 'task'})…", file=sys.stderr)
    md = call_claude(prompt, timeout=240)
    if not md.strip():
        print(json.dumps({"error": "empty output from claude"}), file=sys.stderr)
        return 1

    tc_ids = re.findall(r"^##\s+(TC-[A-Za-z0-9-]+)", md, re.MULTILINE)
    if not tc_ids:
        print(json.dumps({"error": "output has no '## TC-…' headers, not saving",
                          "head": md[:300]}), file=sys.stderr)
        return 1

    out_path.write_text(md if md.endswith("\n") else md + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out_path), "tc_count": len(tc_ids), "tc_ids": tc_ids},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
