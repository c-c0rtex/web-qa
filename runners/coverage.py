"""Control-level coverage — which interactive elements no test case ever names.

Route coverage is a weak proxy. A page with a kanban, a tree/board toggle, a "generate"
action and an XLSX export counts as covered the moment ONE test case navigates to it, even
if that test case only reads a table. The gap is invisible to `route_coverage` by
construction, and `--cover-gaps` will never propose it.

This became computable only once the app map stopped truncating ARIA snapshots to 800
characters: the snapshots now name every button, tab and combobox on every route.

Deterministic, zero tokens.
"""

from __future__ import annotations

import re
from pathlib import Path

# Roles a user ACTS on. `link` is deliberately absent — navigation is route coverage's job,
# and every page's sidebar would otherwise drown the signal.
CONTROL_ROLES = ("button", "tab", "checkbox", "combobox", "switch", "menuitem", "radio",
                 "menuitemcheckbox", "slider")

RE_ARIA_ENTRY = re.compile(r"^### `([^`]+)`.*?\n```yaml\n(.*?)\n```", re.S | re.M)
RE_CONTROL = re.compile(r'^\s*-\s+(?:%s)\s+"([^"]+)"' % "|".join(CONTROL_ROLES), re.M)
# A run of six or more digits is a timestamp or an id: the control's name is data, not UI.
RE_DATA_NAME = re.compile(r"\d{6,}")

CHROME_SHARE = 0.5     # a name on more than half the routes is layout, not a feature


def aria_section(md: str) -> str:
    s = md.find("## ARIA snapshots")
    if s < 0:
        return ""
    e = md.find("\n## ", s + 1)
    return md[s:] if e < 0 else md[s:e]


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


def controls_by_route(md: str) -> dict[str, set[str]]:
    """route → the accessible names of the controls its ARIA snapshot shows."""
    out: dict[str, set[str]] = {}
    for m in RE_ARIA_ENTRY.finditer(aria_section(md)):
        route, body = m.group(1), m.group(2)
        names = {normalize(n) for n in RE_CONTROL.findall(body)}
        out[route] = {n for n in names if n and not RE_DATA_NAME.search(n)}
    return out


def layout_chrome(by_route: dict[str, set[str]], share: float = CHROME_SHARE) -> set[str]:
    """Names present on more than `share` of the routes: the sidebar, the theme switch, the
    user menu. Testing them once is enough; reporting them per route is noise."""
    if len(by_route) < 3:
        return set()
    counts: dict[str, int] = {}
    for names in by_route.values():
        for n in names:
            counts[n] = counts.get(n, 0) + 1
    return {n for n, c in counts.items() if c > share * len(by_route)}


def element_coverage(md: str, tc_bodies: list[str]) -> dict[str, list[str]]:
    """route → control names that no test case anywhere mentions. Empty routes are dropped.

    A name counts as covered when it appears in ANY test case's text, not only in one that
    visits this route: a mention is a mention, and being lenient here keeps the report free
    of things a human would call a false alarm."""
    by_route = controls_by_route(md)
    if not by_route:
        return {}
    chrome = layout_chrome(by_route)
    haystack = normalize(" \n ".join(tc_bodies))
    out: dict[str, list[str]] = {}
    for route, names in by_route.items():
        missing = sorted(n for n in names - chrome if n not in haystack)
        if missing:
            out[route] = missing
    return out


def load_map_and_tcs(webqa: Path) -> tuple[str, list[str]]:
    from run_scenarios import split_tcs
    ctx = webqa / "app.context.md"
    md = ctx.read_text(encoding="utf-8") if ctx.is_file() else ""
    bodies: list[str] = []
    scenarios = webqa / "scenarios"
    for f in sorted(scenarios.glob("*.md")) if scenarios.is_dir() else []:
        bodies += [tc["body"] for tc in split_tcs(f.read_text(encoding="utf-8"))]
    return md, bodies


def control_coverage(webqa: Path) -> dict[str, list[str]]:
    md, bodies = load_map_and_tcs(webqa)
    return element_coverage(md, bodies) if md else {}


def control_gap_section(uncovered: dict[str, list[str]], max_routes: int = 6,
                        max_names: int = 10) -> str:
    """Prompt block naming the untouched mechanics, worst route first."""
    if not uncovered:
        return ""
    ranked = sorted(uncovered.items(), key=lambda kv: -len(kv[1]))[:max_routes]
    lines = ["\nUNTOUCHED CONTROLS (from the app map's ARIA snapshots; no test case names these):"]
    for route, names in ranked:
        shown = ", ".join(f"«{n}»" for n in names[:max_names])
        more = f" (+{len(names) - max_names} more)" if len(names) > max_names else ""
        lines.append(f"- `{route}`: {shown}{more}")
    lines.append("A route with one test case that only reads a table is not a tested route. "
                 "Prefer covering these mechanics over re-testing the basics.")
    return "\n".join(lines)
