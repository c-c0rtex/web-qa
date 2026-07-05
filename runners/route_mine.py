"""Static route mining — enumerate SPA routes from the app's source code.

BFS over <a href> misses client-side navigation (buttons calling router.push, routes
behind state), but those routes are almost always DECLARED in code. This module reads
the declarations — deterministically, zero tokens:

  - file-based routers are enumerated from the directory layout:
      Next.js app router   app/**/page.{js,jsx,ts,tsx}
      Next.js pages router pages/**/*.{js,jsx,ts,tsx}
      Nuxt                 pages/**/*.vue
      SvelteKit            src/routes/**/+page.svelte
  - config-based routers (React Router, Vue Router, Angular) via a bounded regex scan:
      path: '/orders/:id'   and   <Route path="/orders">
    Only absolute paths are taken — relative child routes can't be resolved reliably
    without parsing the whole route tree, and a wrong guess is worse than a miss.

Dynamic segments ([id], :id, [...slug]) are normalized to the crawler's {param} template
form, so they plug into the existing id_discovery / materialize_path machinery.

Mined routes seed the crawl queue (concrete ones are visited directly even when no link
points at them) and expand the coverage denominator in app.context.md.
"""

from __future__ import annotations

import re
from pathlib import Path

PRUNE_DIRS = {"node_modules", ".git", ".next", ".nuxt", ".svelte-kit", ".output",
              "dist", "build", "out", "coverage", "vendor", "__pycache__", ".venv", "venv",
              ".web-qa", "test-results", "storybook-static"}
PAGE_EXTS = (".js", ".jsx", ".ts", ".tsx")
MAX_SCAN_FILES = 800
MAX_FILE_BYTES = 200_000
MAX_ROUTES = 200

RE_PATH_PROP = re.compile(r"""\bpath\s*:\s*['"](/[^'"]*)['"]""")
RE_ROUTE_JSX = re.compile(r"""<Route[^>]*\spath\s*=\s*["'](/[^"']*)["']""")


def _norm_segment(seg: str) -> str | None:
    """Directory/file segment → route segment. None = contributes nothing to the path."""
    if seg.startswith("(") and seg.endswith(")"):      # route group
        return None
    if seg.startswith("@"):                            # parallel-route slot
        return None
    m = re.fullmatch(r"\[\[?(?:\.\.\.)?([^\]]+)\]\]?", seg)   # [id] [...slug] [[...slug]]
    if m:
        return "{" + m.group(1) + "}"
    if seg.startswith("_"):                            # Nuxt2 dynamic _id.vue → {id}
        return "{" + seg[1:] + "}" if len(seg) > 1 else None
    return seg


def _join(segments: list[str]) -> str:
    parts = [s for s in (_norm_segment(x) for x in segments) if s]
    return "/" + "/".join(parts) if parts else "/"


def _colon_params(path: str) -> str:
    """React/Vue Router `:id` params → `{id}`; optional `:id?` too."""
    return re.sub(r":([A-Za-z_][A-Za-z0-9_]*)\??", r"{\1}", path)


def _walk_files(root: Path):
    """All files under root with PRUNE_DIRS subtrees skipped, deterministic order."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir():
                if e.name not in PRUNE_DIRS:
                    stack.append(e)
            else:
                yield e


def _mine_next_app(app_dir: Path) -> list[str]:
    routes = []
    for f in _walk_files(app_dir):
        if f.stem == "page" and f.suffix in PAGE_EXTS:
            rel = f.parent.relative_to(app_dir).parts
            if any(s.startswith("_") for s in rel):    # _private folders opt out of routing
                continue
            routes.append(_join(list(rel)))
    return routes


def _mine_pages_dir(pages_dir: Path) -> list[str]:
    """Next.js pages router (.js/.tsx) and Nuxt (.vue) share the same layout idea."""
    routes = []
    for f in _walk_files(pages_dir):
        if f.suffix not in (*PAGE_EXTS, ".vue"):
            continue
        rel = f.relative_to(pages_dir).parts
        if rel and rel[0] == "api":
            continue
        stem = f.stem
        if stem in ("_app", "_document", "_error", "404", "500", "middleware"):
            continue
        if stem.startswith("_") and f.suffix != ".vue":
            continue  # Next.js: _-prefixed files are not routes; Nuxt2 _id.vue IS dynamic
        segments = list(rel[:-1]) + ([] if stem == "index" else [stem])
        routes.append(_join(segments))
    return routes


def _mine_sveltekit(routes_dir: Path) -> list[str]:
    routes = []
    for f in _walk_files(routes_dir):
        if f.name == "+page.svelte":
            routes.append(_join(list(f.parent.relative_to(routes_dir).parts)))
    return routes


def _mine_router_configs(src_root: Path) -> list[str]:
    routes: list[str] = []
    scanned = 0
    for f in _walk_files(src_root):
        if f.suffix not in (*PAGE_EXTS, ".vue"):
            continue
        scanned += 1
        if scanned > MAX_SCAN_FILES:
            break
        try:
            if f.stat().st_size > MAX_FILE_BYTES:
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx in (RE_PATH_PROP, RE_ROUTE_JSX):
            for m in rx.finditer(text):
                p = _colon_params(m.group(1))
                if "*" not in p:                       # wildcards aren't visitable routes
                    routes.append(p)
    return routes


def _normalize(path: str) -> str:
    path = re.sub(r"/{2,}", "/", path)
    return path.rstrip("/") or "/"


def mine_routes(project_root: Path, frontend_dir: str | None = None) -> list[dict]:
    """Returns [{"path": "/orders/{id}", "source": "next-app"}, ...], deduped and sorted.
    `frontend_dir` (config.json) points at the app inside a monorepo; default: repo root."""
    root = project_root / frontend_dir if frontend_dir else project_root
    if not root.is_dir():
        return []
    found: dict[str, str] = {}

    def add(paths: list[str], source: str) -> None:
        for p in paths:
            p = _normalize(p)
            found.setdefault(p, source)

    for base in (root, root / "src"):
        app_dir = base / "app"
        if app_dir.is_dir():
            add(_mine_next_app(app_dir), "next-app")
        pages_dir = base / "pages"
        if pages_dir.is_dir():
            add(_mine_pages_dir(pages_dir), "pages-dir")
    sveltekit = root / "src" / "routes"
    if sveltekit.is_dir() and any(sveltekit.rglob("+page.svelte")):
        add(_mine_sveltekit(sveltekit), "sveltekit")

    scan_root = root / "src" if (root / "src").is_dir() else root
    add(_mine_router_configs(scan_root), "router-config")

    return [{"path": p, "source": s} for p, s in sorted(found.items())][:MAX_ROUTES]


def as_template(path: str) -> str:
    """Comparison form shared by mined and crawled routes: any {param} → {id},
    concrete numeric segments → {id} (matches normalize_for_dedup's collapse)."""
    path = re.sub(r"\{[^}]+\}", "{id}", path)
    return re.sub(r"/\d+(?=/|$)", "/{id}", path)
