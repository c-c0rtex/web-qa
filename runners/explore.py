"""Exploration runner — login + crawl frontend, output structured app.context.md.

Strategy:
  1. Login via backend API (cookie auth) → save storageState for Playwright
  2. Open frontend with that storage, traverse routes BFS:
     - capture URL, title, h1/h2, key forms (input names + submit buttons), key buttons (data-testid/role=button), table headers
     - extract internal links (same-origin), follow up to MAX_PAGES depth-first
  3. Build app.context.md with sections: Routes, User Roles (from /auth/me), Forms, Tables, Backend Endpoints, Auth Flow.

Inputs:
  --alias <name>    project alias from projects.json
  --login email password  (optional; if absent, project config.json's auth fields are used)
  --max-pages 30    crawl cap

Output: <project>/.web-qa/app.context.md — regenerated on every run, EXCEPT everything
below the `<!-- manual -->` marker, which survives re-crawls (hand-written notes live there).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from playwright.sync_api import sync_playwright


def load_project(alias: str) -> dict:
    """Registry entry merged with <project>/.web-qa/config.json (project keys win, None ignored)."""
    reg = Path(__file__).resolve().parent.parent / "projects.json"
    entries = json.loads(reg.read_text())
    for e in entries:
        if e.get("alias") == alias:
            proj = dict(e)
            cfg_path = Path(proj.get("path", "")) / ".web-qa" / "config.json"
            if cfg_path.is_file():
                try:
                    cfg = json.loads(cfg_path.read_text())
                except json.JSONDecodeError as err:
                    raise SystemExit(f"invalid JSON in {cfg_path}: {err}")
                for k, v in cfg.items():
                    if v is not None:
                        proj[k] = v
            return proj
    raise SystemExit(f"alias {alias!r} not in registry")


def resolve_credentials(proj: dict, email: str | None, password: str | None,
                        role: str | None = None) -> tuple[str, str]:
    """CLI args take priority, then the named role from `roles`, then default `auth`.
    No hardcoded fallbacks."""
    if role:
        for r in proj.get("roles") or []:
            if (r.get("name") or "").lower() == role.lower():
                email = email or r.get("email")
                password = password or r.get("password")
                break
        else:
            known = [r.get("name") for r in proj.get("roles") or []]
            raise SystemExit(f"role {role!r} not found for {proj.get('alias')!r}; known roles: {known}")
    auth = proj.get("auth") or {}
    email = email or auth.get("email")
    password = password or auth.get("password")
    if not email or not password:
        raise SystemExit(
            f"no credentials for {proj.get('alias')!r}: pass --email/--password "
            f"or set \"auth\": {{\"email\", \"password\"}} (and optionally \"roles\") in projects.json"
        )
    return email, password


DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
MANUAL_MARKER = "<!-- manual -->"


def project_viewport(proj: dict) -> dict:
    """Per-project `viewport` from config, with a consistent default across all layers."""
    vp = proj.get("viewport") or {}
    return {"width": int(vp.get("width", DEFAULT_VIEWPORT["width"])),
            "height": int(vp.get("height", DEFAULT_VIEWPORT["height"]))}


def viewport_entries(proj: dict) -> list[dict]:
    """Named viewports from config `viewports`; falls back to the single `viewport`
    key (v0.2) or the default. First entry is the project default."""
    vps = proj.get("viewports")
    if vps:
        return vps
    return [{"name": "default", **project_viewport(proj)}]


def viewport_env(proj: dict) -> str | None:
    """WIDTHxHEIGHT string for the WEBQA_VIEWPORT env var (specs config), or None when the
    project sets no viewport at all. Honors both the single `viewport` key and the first
    (default) entry of `viewports`; device entries return None — their size comes from the
    device descriptor via WEBQA_MOBILE_DEVICE."""
    if not (proj.get("viewport") or proj.get("viewports")):
        return None
    e = viewport_entries(proj)[0]
    if e.get("device"):
        return None
    return (f"{int(e.get('width', DEFAULT_VIEWPORT['width']))}"
            f"x{int(e.get('height', DEFAULT_VIEWPORT['height']))}")


def viewport_entry(proj: dict, name: str | None = None) -> dict:
    entries = viewport_entries(proj)
    if not name:
        return entries[0]
    for e in entries:
        if e.get("name") == name:
            return e
    known = [e.get("name") for e in entries]
    raise SystemExit(f"viewport {name!r} not found; known viewports: {known}")


def context_kwargs_for(entry: dict, playwright) -> dict:
    """Playwright new_context kwargs for a viewport entry. `device` entries use the
    full descriptor (touch, user-agent, deviceScaleFactor) — real mobile emulation,
    not just a narrow window."""
    device = entry.get("device")
    if device:
        descriptor = playwright.devices.get(device)
        if not descriptor:
            raise SystemExit(f"unknown Playwright device {device!r} (see playwright.devices)")
        return dict(descriptor)
    return {"viewport": {"width": int(entry.get("width", DEFAULT_VIEWPORT["width"])),
                         "height": int(entry.get("height", DEFAULT_VIEWPORT["height"]))}}


def viewport_suffix(proj: dict, name: str | None) -> str:
    """Baseline/report suffix: the project-default viewport keeps unsuffixed names
    (backward compatible), every other viewport gets `@<name>`."""
    if not name or name == viewport_entries(proj)[0].get("name"):
        return ""
    return f"@{name}"


def merge_manual_section(new_md: str, existing_md: str | None) -> str:
    """Everything below MANUAL_MARKER in app.context.md survives re-crawls.
    First write scaffolds the marker so the feature is discoverable."""
    if existing_md and MANUAL_MARKER in existing_md:
        manual = existing_md[existing_md.index(MANUAL_MARKER):].rstrip()
    else:
        manual = (MANUAL_MARKER + "\n<!-- Everything below this marker survives `web-qa-explore` re-crawls.\n"
                  "     Add business rules, roles, corner cases the crawler can't see. -->")
    return new_md.rstrip() + "\n\n" + manual + "\n"


def api_login(backend_url: str, email: str, password: str) -> tuple[dict, dict]:
    """Return (cookies_dict, user_me_dict)."""
    r = httpx.post(f"{backend_url}/auth/login", json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return dict(r.cookies), r.json()


def fetch_openapi(backend_url: str) -> dict:
    try:
        return httpx.get(f"{backend_url}/openapi.json", timeout=5).json()
    except Exception:
        return {}


def cookies_to_storage_state(cookies: dict, target_url: str) -> dict:
    """Build a Playwright storageState dict from raw cookies dict."""
    parsed = urlparse(target_url)
    domain = parsed.hostname or "localhost"
    return {
        "cookies": [
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
            for name, value in cookies.items()
        ],
        "origins": [],
    }


def extract_page_summary(page) -> dict:
    """Capture key elements on the current page."""
    js = """
    () => {
      const text = (s) => (s || '').trim().replace(/\\s+/g, ' ').slice(0, 200);
      const headings = Array.from(document.querySelectorAll('h1, h2'))
        .slice(0, 8)
        .map((el) => ({ tag: el.tagName, text: text(el.textContent) }));
      const forms = Array.from(document.querySelectorAll('form')).slice(0, 6).map((f) => ({
        action: f.getAttribute('action') || '',
        method: f.method || 'get',
        inputs: Array.from(f.querySelectorAll('input, select, textarea'))
          .slice(0, 12)
          .map((i) => ({ name: i.name || i.getAttribute('name') || '', type: i.type || i.tagName.toLowerCase(), placeholder: i.placeholder || '' })),
        buttons: Array.from(f.querySelectorAll('button, [type="submit"]'))
          .slice(0, 6)
          .map((b) => text(b.textContent) || b.getAttribute('aria-label') || ''),
      }));
      const buttons = Array.from(document.querySelectorAll('button, [role="button"]'))
        .slice(0, 20)
        .map((b) => ({
          text: text(b.textContent),
          testid: b.getAttribute('data-testid') || '',
          aria: b.getAttribute('aria-label') || '',
        }))
        .filter((b) => b.text || b.testid || b.aria);
      const tables = Array.from(document.querySelectorAll('table')).slice(0, 4).map((t) => ({
        headers: Array.from(t.querySelectorAll('th')).slice(0, 16).map((th) => text(th.textContent)),
        rowCount: t.querySelectorAll('tbody tr').length,
      }));
      const links = Array.from(document.querySelectorAll('a[href]'))
        .slice(0, 80)
        .map((a) => ({
          href: a.getAttribute('href') || '',
          text: text(a.textContent),
        }))
        .filter((l) => l.href && !l.href.startsWith('javascript:') && !l.href.startsWith('mailto:'));
      return {
        title: text(document.title),
        url: location.href,
        path: location.pathname + location.search,
        headings, forms, buttons, tables, links,
      };
    }
    """
    return page.evaluate(js)


def normalize_for_dedup(url: str) -> str:
    """Dedup key: strip query/fragment, collapse numeric path segments to {id}.
    /orders?page=2 and /orders are one page; /orders/17 and /orders/42 are one template."""
    parsed = urlparse(url.split("#")[0])
    path = re.sub(r"/\d+(?=/|$)", "/{id}", parsed.path)
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def crawl(target_url: str, storage_state: dict, max_pages: int = 30,
          per_template: int = 2, vp_entry: dict | None = None) -> list[dict]:
    """BFS over same-origin URLs, return list of page summaries.
    Visits at most `per_template` concrete URLs per normalized route template so
    entity cards (/orders/1, /orders/2, …) don't eat the whole max_pages budget."""
    parsed = urlparse(target_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    visited: set[str] = set()
    template_counts: dict[str, int] = {}
    queue: deque[str] = deque([target_url])
    pages: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        kwargs = context_kwargs_for(vp_entry, p) if vp_entry else {"viewport": DEFAULT_VIEWPORT}
        ctx = browser.new_context(storage_state=storage_state, **kwargs)
        page = ctx.new_page()

        while queue and len(pages) < max_pages:
            url = queue.popleft()
            key = normalize_for_dedup(url)
            if "{id}" in key:
                # entity-card template: allow up to per_template distinct concrete URLs
                exact = url.split("#")[0]
                if exact in visited or template_counts.get(key, 0) >= per_template:
                    continue
                visited.add(exact)
                template_counts[key] = template_counts.get(key, 0) + 1
            else:
                if key in visited:
                    continue
                visited.add(key)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                try:
                    page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass  # SPA with polling — bounded wait is enough
            except Exception as e:
                pages.append({"url": url, "error": str(e)})
                continue
            try:
                summary = extract_page_summary(page)
            except Exception as e:
                pages.append({"url": url, "error": f"extract failed: {e}"})
                continue
            try:
                # role/name ground truth for getByRole — far better selector grounding
                # than our element tables alone (idea borrowed from Playwright's agents)
                summary["aria"] = page.locator("body").aria_snapshot()[:800]
            except Exception:
                pass
            pages.append(summary)

            # Enqueue same-origin internal links
            for link in summary.get("links", []):
                href = link["href"]
                target = urljoin(url, href)
                t = urlparse(target)
                if f"{t.scheme}://{t.netloc}" != origin:
                    continue
                tkey = normalize_for_dedup(target)
                if "{id}" in tkey:
                    if target.split("#")[0] in visited or template_counts.get(tkey, 0) >= per_template:
                        continue
                elif tkey in visited:
                    continue
                queue.append(target)

        browser.close()
    return pages


def render_context_md(project: dict, pages: list[dict], openapi: dict, user_me: dict) -> str:
    lines: list[str] = []
    alias = project.get("alias", "project")
    lines.append(f"# {alias} — App Context\n")
    lines.append(f"<i>Auto-generated by web-qa Exploration on {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}.</i>\n")
    lines.append(f"- Frontend: <{project['target_url']}>")
    if project.get("backend_url"):
        lines.append(f"- Backend: <{project['backend_url']}>")
    lines.append(f"- Logged-in role: **{user_me.get('role', '?')}** ({user_me.get('email', '?')})")
    lines.append("")

    # ===== Routes =====
    lines.append("## Routes (frontend)\n")
    lines.append("| Path | Title | h1/h2 | Forms | Tables | Buttons |")
    lines.append("|---|---|---|---|---|---|")
    for p in pages:
        if "error" in p and "url" in p:
            lines.append(f"| {urlparse(p['url']).path} | _error_ | {p['error'][:60]} | — | — | — |")
            continue
        path = p.get("path", "")
        title = (p.get("title") or "").replace("|", "\\|")[:60]
        head = " / ".join(h["text"] for h in p.get("headings", [])[:3]).replace("|", "\\|")[:80]
        forms_count = len(p.get("forms", []))
        tables_count = len(p.get("tables", []))
        btns = len(p.get("buttons", []))
        lines.append(f"| `{path}` | {title} | {head} | {forms_count} | {tables_count} | {btns} |")
    lines.append("")

    # ===== Forms =====
    lines.append("## Forms (per route)\n")
    for p in pages:
        forms = p.get("forms") or []
        if not forms:
            continue
        path = p.get("path", "")
        for i, f in enumerate(forms, 1):
            inputs = ", ".join(f"{i.get('name') or i.get('placeholder') or '?'}({i.get('type')})" for i in f.get("inputs", []))
            buttons = ", ".join(b for b in f.get("buttons", []) if b)
            lines.append(f"- `{path}` form#{i}: action=`{f.get('action','')}` method={f.get('method','')}; inputs: {inputs}; buttons: {buttons}")
    lines.append("")

    # ===== Tables =====
    lines.append("## Tables (per route)\n")
    has_any = False
    for p in pages:
        for t in p.get("tables") or []:
            has_any = True
            path = p.get("path", "")
            lines.append(f"- `{path}`: headers=[{', '.join(t.get('headers', []))}], rows={t.get('rowCount', 0)}")
    if not has_any:
        lines.append("_(no tables encountered)_")
    lines.append("")

    # ===== ARIA snapshots =====
    lines.append("## ARIA snapshots (role/name — ground truth for getByRole)\n")
    aria_budget = 8000
    used = 0
    any_aria = False
    for p in pages:
        a = p.get("aria")
        if not a:
            continue
        if used + len(a) > aria_budget:
            lines.append("_(aria budget reached — remaining routes omitted)_")
            break
        any_aria = True
        used += len(a)
        lines.append(f"### `{p.get('path', '')}`\n```yaml\n{a}\n```")
    if not any_aria:
        lines.append("_(no aria snapshots captured)_")
    lines.append("")

    # ===== Backend endpoints =====
    lines.append("## Backend endpoints (from OpenAPI)\n")
    paths = openapi.get("paths", {}) if openapi else {}
    if paths:
        def schema_fields(schema: dict) -> str:
            """Flatten a request schema into `field*:type` (star = required). Resolves one $ref."""
            if "$ref" in schema:
                name = schema["$ref"].split("/")[-1]
                schema = (openapi.get("components", {}).get("schemas", {}) or {}).get(name, {})
            props = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            out = []
            for k, v in list(props.items())[:20]:
                t = v.get("type") or ("$ref" if "$ref" in v else "any")
                out.append(f"{k}{'*' if k in required else ''}:{t}")
            return ", ".join(out)

        # Group by first segment; for mutating methods include REAL request field names so
        # spec-gen stops guessing the API contract (mode vs transport_mode etc.)
        groups: dict[str, list[str]] = {}
        for p, methods in paths.items():
            parts = p.strip("/").split("/")
            head = parts[0] if parts else "root"
            entry = [p + " — " + ", ".join(m.upper() for m in methods.keys() if m in ("get", "post", "put", "patch", "delete"))]
            for m in ("post", "put", "patch"):
                body_schema = (((methods.get(m) or {}).get("requestBody") or {})
                               .get("content", {}).get("application/json", {}).get("schema"))
                if body_schema:
                    fields = schema_fields(body_schema)
                    if fields:
                        entry.append(f"  - {m.upper()} body: {fields}")
            groups.setdefault(head, []).append("\n".join(entry))
        for head, items in sorted(groups.items()):
            lines.append(f"### `/{head}`")
            for it in items:
                lines.append(f"- {it}")
            lines.append("")
    else:
        lines.append("_(openapi not reachable)_\n")

    # ===== Auth flow =====
    lines.append("## Auth Flow\n")
    auth_notes = project.get("auth_flow_notes") or []
    if auth_notes:
        lines.extend(f"- {n}" for n in auth_notes)
    else:
        # Derive what we can from OpenAPI; the rest is up to the human
        auth_paths = [p for p in (openapi.get("paths") or {}) if p.startswith("/auth")]
        if auth_paths:
            for p in sorted(auth_paths):
                methods = ", ".join(m.upper() for m in openapi["paths"][p] if m in ("get", "post", "put", "patch", "delete"))
                lines.append(f"- `{p}` — {methods}")
        else:
            lines.append("_(no auth endpoints detected; set `auth_flow_notes` in `.web-qa/config.json`)_")
    lines.append("")

    # ===== Notes =====
    lines.append("## Out of Scope / Notes\n")
    context_notes = project.get("context_notes") or []
    if context_notes:
        lines.extend(f"- {n}" for n in context_notes)
    else:
        lines.append("_(none; set `context_notes` in `.web-qa/config.json` for project-specific caveats)_")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    ap.add_argument("--email")
    ap.add_argument("--password")
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--viewport", help="named viewport from config `viewports` "
                                       "(non-default writes app.context.<name>.md)")
    args = ap.parse_args()

    proj = load_project(args.alias)
    target = proj["target_url"]
    backend = proj.get("backend_url") or target
    email, password = resolve_credentials(proj, args.email, args.password)

    print(f"[explore] login → {backend}", file=sys.stderr)
    cookies, user_me = api_login(backend, email, password)
    print(f"[explore] logged in as {user_me.get('email')} ({user_me.get('role')})", file=sys.stderr)

    storage = cookies_to_storage_state(cookies, target)
    openapi = fetch_openapi(backend)

    entry = viewport_entry(proj, args.viewport)
    suffix = viewport_suffix(proj, args.viewport)
    label = entry.get("device") or (f"{entry.get('width', DEFAULT_VIEWPORT['width'])}"
                                    f"x{entry.get('height', DEFAULT_VIEWPORT['height'])}")
    print(f"[explore] crawling {target} (max_pages={args.max_pages}, viewport={label})", file=sys.stderr)
    pages = crawl(target, storage, max_pages=args.max_pages, vp_entry=entry)
    print(f"[explore] crawled {len(pages)} pages", file=sys.stderr)

    md = render_context_md(proj, pages, openapi, user_me)
    if suffix:
        md = md.replace("— App Context", f"— App Context ({entry.get('name')} viewport)", 1)
    out_name = f"app.context{suffix.replace('@', '.')}.md" if suffix else "app.context.md"
    out = Path(proj["path"]) / ".web-qa" / out_name
    existing = out.read_text() if out.is_file() else None
    out.write_text(merge_manual_section(md, existing))
    print(json.dumps({"alias": args.alias, "pages_crawled": len(pages), "out": str(out), "size": out.stat().st_size}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
