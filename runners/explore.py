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
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from playwright.sync_api import sync_playwright

from registry import registry_path
from route_mine import as_template, mine_routes


def load_project(alias: str) -> dict:
    """Registry entry merged with <project>/.web-qa/config.json (project keys win, None ignored).
    Registry resolution (env override, skill root, XDG for plugin installs) lives in registry.py."""
    reg = registry_path()
    if not reg.is_file():
        raise SystemExit(f"no registry at {reg} — run web-qa-register-project first")
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


def _render_body(tpl, subs: dict):
    """Recursively substitute {email}/{password} placeholders in a JSON body template."""
    if isinstance(tpl, str):
        for k, v in subs.items():
            tpl = tpl.replace("{" + k + "}", v)
        return tpl
    if isinstance(tpl, dict):
        return {k: _render_body(v, subs) for k, v in tpl.items()}
    if isinstance(tpl, list):
        return [_render_body(v, subs) for v in tpl]
    return tpl


def _dig(obj, dotted: str):
    """`user.token` → obj["user"]["token"], None on any miss."""
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def run_fixture_cmd(proj: dict, *, teardown: bool = False) -> None:
    """Run the project's `fixture_cmd` / `fixture_teardown_cmd` (config.json) from the
    project root. Deterministic seeded data is what makes visual regression possible on
    data-driven pages and keeps specs independent of leftover state. A non-zero exit is
    a hard stop: verdicts from a half-seeded stand can't be trusted."""
    import subprocess
    key = "fixture_teardown_cmd" if teardown else "fixture_cmd"
    cmd = proj.get(key)
    if not cmd:
        return
    print(f"[fixtures] {key}: {cmd}", file=sys.stderr)
    proc = subprocess.run(cmd, shell=True, cwd=proj.get("path") or ".", timeout=600)
    if proc.returncode != 0:
        raise SystemExit(f"{key} failed (exit {proc.returncode}): {cmd}")


def api_login(backend_url: str, email: str, password: str,
              proj: dict | None = None) -> tuple[dict, dict, str | None]:
    """Return (cookies_dict, user_me_dict, token_or_None).

    Config-driven auth adapter — the login contract differs per app and is declared in
    config.json instead of being hardcoded:
      auth_login_path   login endpoint (default /auth/login)
      auth_login_body   JSON body template with {email}/{password} (default flat)
      auth_token_field  dot-path to a bearer token in the response (e.g. "user.token")
    Cookie-session apps need none of these — the defaults reproduce the old behavior."""
    proj = proj or {}
    path = proj.get("auth_login_path") or "/auth/login"
    body_tpl = proj.get("auth_login_body") or {"email": "{email}", "password": "{password}"}
    body = _render_body(body_tpl, {"email": email, "password": password})
    r = httpx.post(f"{backend_url}{path}", json=body, timeout=10)
    r.raise_for_status()
    data = r.json()
    token_field = proj.get("auth_token_field")
    token = str(_dig(data, token_field)) if token_field and _dig(data, token_field) else None
    return dict(r.cookies), data, token


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


def build_storage_state(cookies: dict, target_url: str, token: str | None = None,
                        proj: dict | None = None) -> dict:
    """Playwright storageState: cookies always; plus a localStorage entry when the app
    keeps its auth token there (config `auth_browser_storage`:
    {"kind": "localStorage", "key": "<ls key>", "value": "{token}"}). SPAs like RealWorld
    never see a session cookie — without this the crawler browses logged-out."""
    state = cookies_to_storage_state(cookies, target_url)
    bs = (proj or {}).get("auth_browser_storage") or {}
    if token and bs.get("kind") == "localStorage" and bs.get("key"):
        parsed = urlparse(target_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        value = (bs.get("value") or "{token}").replace("{token}", token)
        state["origins"] = [{"origin": origin,
                             "localStorage": [{"name": bs["key"], "value": value}]}]
    return state


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
        path: location.pathname,   // the app may replaceState a ?loaded=N onto the URL
        headings, forms, buttons, tables, links,
      };
    }
    """
    return page.evaluate(js)


def normalize_for_dedup(url: str) -> str:
    """Dedup key: strip query/fragment, collapse numeric path segments to {id}.
    /orders?page=2 and /orders are one page; /orders/17 and /orders/42 are one template."""
    parsed = urlparse(url.split("#")[0])
    path = re.sub(r"/\d+(?=/|$)", "/{id}", parsed.path) or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


INTERACTIVE_CLICKS_PER_PAGE = 12


def interactive_discover(page, origin: str) -> list[str]:
    """Opt-in pass for apps with runtime-only navigation: click non-link clickables and
    harvest pushState URL changes. Mutation safety is enforced at the NETWORK level, not
    by guessing button semantics from text — every non-GET request is aborted for the
    duration of the pass, so a "Delete" button physically cannot reach the backend.
    Known limits (documented in SKILL.md): the valve covers what Chromium routes through
    request interception — WebSocket frames on already-open connections bypass it, and a
    GET with server-side side effects (an anti-pattern, but real) is let through."""
    discovered: list[str] = []
    base_url = page.url

    def guard(route):
        if route.request.method in ("GET", "HEAD", "OPTIONS"):
            route.continue_()
        else:
            route.abort()

    def on_dialog(dialog):
        dialog.dismiss()

    page.route("**/*", guard)
    page.on("dialog", on_dialog)
    try:
        sel = "button, [role=button], [role=tab], [role=menuitem]"
        count = min(page.locator(sel).count(), INTERACTIVE_CLICKS_PER_PAGE)
        for i in range(count):
            try:
                page.locator(sel).nth(i).click(timeout=1500)
                page.wait_for_timeout(400)
            except Exception:
                continue
            if page.url == base_url:
                continue
            t = urlparse(page.url)
            if f"{t.scheme}://{t.netloc}" == origin and page.url not in discovered:
                discovered.append(page.url)
            try:
                page.go_back(wait_until="domcontentloaded", timeout=5000)
            except Exception:
                pass
            if page.url != base_url:
                page.goto(base_url, wait_until="domcontentloaded", timeout=10000)
    finally:
        page.remove_listener("dialog", on_dialog)
        page.unroute("**/*")
    return discovered


def crawl(target_url: str, storage_state: dict, max_pages: int = 30,
          per_template: int = 2, vp_entry: dict | None = None,
          seed_paths: list[str] | None = None, interactive: bool = False) -> list[dict]:
    """BFS over same-origin URLs, return list of page summaries.
    Visits at most `per_template` concrete URLs per normalized route template so
    entity cards (/orders/1, /orders/2, …) don't eat the whole max_pages budget.
    `seed_paths` (statically mined routes) are enqueued up front — they get visited
    even when no crawled page links to them. `interactive` adds the click pass."""
    parsed = urlparse(target_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    visited: set[str] = set()
    template_counts: dict[str, int] = {}
    queue: deque[str] = deque([target_url])
    for sp in seed_paths or []:
        queue.append(origin + sp)
    pages: list[dict] = []

    def try_enqueue(target: str) -> None:
        t = urlparse(target)
        if f"{t.scheme}://{t.netloc}" != origin:
            return
        if is_static_asset(t.path):
            return
        # dedup already collapses the query, so visiting `/orders?loaded=14` and `/orders`
        # was always the same page — enqueue the canonical form and keep it out of the map
        target = urlunparse(t._replace(query="", fragment=""))
        tkey = normalize_for_dedup(target)
        if "{id}" in tkey:
            if target.split("#")[0] in visited or template_counts.get(tkey, 0) >= per_template:
                return
        elif tkey in visited:
            return
        queue.append(target)

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
            # SPA redirected us elsewhere (e.g. /login → / for an authenticated session):
            # record the redirect instead of duplicating the landing page's row
            landed_key = normalize_for_dedup(page.url)
            if landed_key != key:
                if landed_key in visited:
                    pages.append({"path": urlparse(url).path or "/",
                                  "redirected_to": urlparse(page.url).path or "/"})
                    continue
                visited.add(landed_key)
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
                try_enqueue(urljoin(url, link["href"]))

            if interactive:
                try:
                    for found in interactive_discover(page, origin):
                        try_enqueue(found)
                except Exception as e:
                    print(f"[explore] interactive pass failed on {url}: {e}", file=sys.stderr)

        browser.close()
    return pages


STATIC_EXT = re.compile(
    r"\.(?:png|jpe?g|gif|svg|webp|avif|ico|bmp|pdf|zip|css|js|mjs|map|woff2?|ttf|eot|"
    r"mp4|webm|mp3|xlsx?|docx?|csv|txt)$", re.IGNORECASE)


def is_static_asset(path: str) -> bool:
    """A screenshot is not a route. The crawler followed every same-origin `<a href>`, so a
    docs page linking to 40 PNGs added 40 rows to the Routes table — half the map — and took
    40 of the 74 ARIA slots, halving the snapshot every real page got from the shared budget."""
    return bool(STATIC_EXT.search(urlparse(path).path))


def sidecar_path(out: Path) -> Path:
    """app.context.md → app.context.json — the machine-readable twin the next crawl
    merges against. Parsing the rendered markdown back would be fragile."""
    return out.with_suffix(".json")


def load_prev_pages(out: Path) -> list[dict]:
    p = sidecar_path(out)
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("pages", [])
    except (json.JSONDecodeError, OSError):
        return []


def page_key(p: dict) -> str:
    """Canonical route key. An SPA that replaceStates `?loaded=14` onto the URL must not
    register a second route — dedup already treats both as the same page."""
    k = p.get("path") or p.get("url") or ""
    return k.split("#")[0].split("?")[0] or ("/" if k else "")


def merge_pages(prev: list[dict], fresh: list[dict], today: str) -> tuple[list[dict], dict]:
    """Union by route — a crawl that did not reach a route must not delete it.

    The auto part of app.context.md is rewritten from scratch on every crawl, so a lower
    --max-pages, an expired session or one slow page silently replaced a good map with a
    worse one. Every spec generated afterwards then guessed its selectors. Carrying the
    previous entry over (marked stale) keeps ground truth that specs already depend on."""
    fresh_by = {page_key(p): p for p in fresh if page_key(p)}
    fresh_templates = {as_template(k) for k in fresh_by}
    merged = list(fresh)
    carried: list[str] = []
    for p in prev:
        k = page_key(p)
        if not k or k in fresh_by or p.get("uncrawled"):
            continue
        if is_static_asset(k):
            continue      # an earlier crawl mistook assets for routes; don't resurrect them
        if as_template(k) in fresh_templates:
            continue      # `/orders/23` from last week adds nothing once `/orders/25` is mapped
        q = dict(p)
        q["stale_since"] = p.get("stale_since") or today
        carried.append(k)
        merged.append(q)
    prev_keys = {page_key(p) for p in prev}
    lost_aria = sorted(
        page_key(p) for p in prev
        if p.get("aria") and page_key(p) in fresh_by and not fresh_by[page_key(p)].get("aria")
    )
    report = {
        "carried_over": sorted(carried),
        "new_routes": sorted(k for k in fresh_by if k and k not in prev_keys),
        "lost_aria": lost_aria,
    }
    return merged, report


def annotate_origins(pages: list[dict], mined: list[dict]) -> list[dict]:
    """Mark each crawled page with where the route is known from, and append rows for
    code-declared routes the crawl never reached. Those still belong in the map — and in
    the coverage denominator: a declared route no link leads to is a finding, not noise."""
    mined_by_tpl = {as_template(m["path"]): m for m in mined}
    seen_tpls: set[str] = set()
    for p in pages:
        if "path" not in p:
            continue
        tpl = as_template(p["path"].split("?")[0])
        seen_tpls.add(tpl)
        if p.get("stale_since"):
            # carried over from an earlier crawl — say so rather than claim we just saw it
            p["origin"] = f"stale:{p['stale_since']}"
            continue
        p["origin"] = "crawl+code" if tpl in mined_by_tpl else "crawl"
    extra = [{"path": m["path"], "origin": f"code:{m['source']}", "uncrawled": True}
             for tpl, m in mined_by_tpl.items() if tpl not in seen_tpls]
    return pages + sorted(extra, key=lambda e: e["path"])


def dedupe_by_template(pages: list[dict]) -> list[dict]:
    """One entry per route template for RENDERING (the sidecar keeps every concrete page).

    `/orders/23` and `/orders/24` are the same route, and the coverage denominator already
    collapses them — so the map should speak the same language. A map that advertises
    `/orders/23` also invites the spec to hardcode it, two lines after the prompt forbade it.
    An ARIA snapshot still has to come from a real page, so we name the one it was sampled
    from."""
    seen: set[str] = set()
    out: list[dict] = []
    for p in pages:
        path = p.get("path")
        if not path:
            out.append(p)          # error rows carry only `url`
            continue
        key = as_template(path)          # dedup key: /orders/23 and /orders/{orderId} are one route
        if key in seen:
            continue
        seen.add(key)
        q = dict(p)
        # display: keep an author-declared param name (`/help/{section}` says more than
        # `/help/{id}`); collapse only the concrete ids a crawl happened to land on
        q["template"] = path if "{" in path else re.sub(r"/\d+(?=/|$)", "/{id}", path)
        if q["template"] != path:
            q["sampled_from"] = path
        out.append(q)
    return out


def render_context_md(project: dict, pages: list[dict], openapi: dict, user_me: dict) -> str:
    pages = dedupe_by_template(pages)
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
    lines.append("| Path | Title | h1/h2 | Forms | Tables | Buttons | Origin |")
    lines.append("|---|---|---|---|---|---|---|")
    for p in pages:
        if "error" in p and "url" in p:
            lines.append(f"| {urlparse(p['url']).path} | _error_ | {p['error'][:60]} | — | — | — | crawl |")
            continue
        if p.get("redirected_to"):
            lines.append(f"| `{p.get('template') or p['path']}` | — | _redirects to `{p['redirected_to']}`_ "
                         f"| — | — | — | {p.get('origin', 'crawl')} |")
            continue
        if p.get("uncrawled"):
            lines.append(f"| `{p.get('template') or p['path']}` | — | _declared in code, not reached by crawl_ "
                         f"| — | — | — | {p['origin']} |")
            continue
        path = p.get("template") or p.get("path", "")
        title = (p.get("title") or "").replace("|", "\\|")[:60]
        head = " / ".join(h["text"] for h in p.get("headings", [])[:3]).replace("|", "\\|")[:80]
        forms_count = len(p.get("forms", []))
        tables_count = len(p.get("tables", []))
        btns = len(p.get("buttons", []))
        lines.append(f"| `{path}` | {title} | {head} | {forms_count} | {tables_count} | {btns} "
                     f"| {p.get('origin', 'crawl')} |")
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
    # Budget shared EVENLY, not first-come. Spending it in crawl order gave the first ten
    # routes a full snapshot and the rest — including the most-tested sections — nothing at
    # all, so their specs guessed every selector and hung on the first miss. A short snapshot
    # everywhere beats a long one for a tenth of the app.
    ARIA_BUDGET = 16000
    ARIA_MIN = 400
    with_aria = [p for p in pages if p.get("aria")]
    if not with_aria:
        lines.append("_(no aria snapshots captured)_")
    else:
        per_page = max(ARIA_MIN, ARIA_BUDGET // len(with_aria))
        for p in with_aria:
            a = p["aria"]
            clipped = a[:per_page]
            note = "\n# …(snapshot clipped)" if len(a) > per_page else ""
            route = p.get("template") or p.get("path", "")
            sampled = f"\n_(sampled from `{p['sampled_from']}`)_" if p.get("sampled_from") else ""
            lines.append(f"### `{route}`{sampled}\n```yaml\n{clipped}{note}\n```")
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
    ap.add_argument("--fresh", action="store_true",
                    help="rebuild the map from this crawl alone; do not carry over routes "
                         "the crawl did not reach (default: merge with the previous map)")
    ap.add_argument("--viewport", help="named viewport from config `viewports` "
                                       "(non-default writes app.context.<name>.md)")
    ap.add_argument("--interactive", action="store_true",
                    help="click pass for runtime-only navigation (pushState routes without "
                         "<a href>); non-GET requests are aborted during the pass")
    ap.add_argument("--no-mine", action="store_true",
                    help="skip static route mining from the project source")
    args = ap.parse_args()

    proj = load_project(args.alias)
    target = proj["target_url"]
    backend = proj.get("backend_url") or target
    email, password = resolve_credentials(proj, args.email, args.password)

    print(f"[explore] login → {backend}", file=sys.stderr)
    cookies, user_me, token = api_login(backend, email, password, proj)
    print(f"[explore] logged in as {user_me.get('email')} ({user_me.get('role')})", file=sys.stderr)

    storage = build_storage_state(cookies, target, token, proj)
    openapi = fetch_openapi(backend)

    entry = viewport_entry(proj, args.viewport)
    suffix = viewport_suffix(proj, args.viewport)
    label = entry.get("device") or (f"{entry.get('width', DEFAULT_VIEWPORT['width'])}"
                                    f"x{entry.get('height', DEFAULT_VIEWPORT['height'])}")
    mined: list[dict] = []
    if not args.no_mine:
        mined = mine_routes(Path(proj["path"]), proj.get("frontend_dir"))
        if mined:
            print(f"[explore] mined {len(mined)} route(s) from source "
                  f"({', '.join(sorted({m['source'] for m in mined}))})", file=sys.stderr)
    # concrete mined routes seed the queue; parametrized ones can't be built into a URL
    # without ids, but still land in the map (and the coverage denominator) via annotate
    seeds = [m["path"] for m in mined if "{" not in m["path"]]

    if mined and args.max_pages < len(mined):
        # the route count is known BEFORE the crawl — a cap below it guarantees blind spots
        print(f"[explore] WARNING: --max-pages {args.max_pages} < {len(mined)} routes mined "
              f"from source. Routes beyond the cap get no DOM, and specs for them will guess "
              f"their selectors. Raise --max-pages to at least {len(mined)}.", file=sys.stderr)

    print(f"[explore] crawling {target} (max_pages={args.max_pages}, viewport={label}"
          f"{', interactive' if args.interactive else ''})", file=sys.stderr)
    pages = crawl(target, storage, max_pages=args.max_pages, vp_entry=entry,
                  seed_paths=seeds, interactive=args.interactive)
    crawled_count = len(pages)
    print(f"[explore] crawled {crawled_count} pages", file=sys.stderr)

    out_name = f"app.context{suffix.replace('@', '.')}.md" if suffix else "app.context.md"
    out = Path(proj["path"]) / ".web-qa" / out_name

    merge_report = {"carried_over": [], "new_routes": [], "lost_aria": []}
    if not args.fresh:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        pages, merge_report = merge_pages(load_prev_pages(out), pages, today)
    if merge_report["carried_over"]:
        print(f"[explore] {len(merge_report['carried_over'])} route(s) not reached this crawl, "
              f"carried over from the previous map: "
              f"{', '.join(merge_report['carried_over'])}", file=sys.stderr)
    if merge_report["lost_aria"]:
        # a route we DID reach but whose snapshot vanished — the map got worse, say it out loud
        print(f"[explore] REGRESSION: aria snapshot lost for "
              f"{', '.join(merge_report['lost_aria'])}", file=sys.stderr)

    pages = annotate_origins(pages, mined)

    md = render_context_md(proj, pages, openapi, user_me)
    if suffix:
        md = md.replace("— App Context", f"— App Context ({entry.get('name')} viewport)", 1)
    existing = out.read_text() if out.is_file() else None
    out.write_text(merge_manual_section(md, existing))
    sidecar_path(out).write_text(
        json.dumps({"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "pages": pages}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"alias": args.alias, "pages_crawled": crawled_count,
                      "routes_mined": len(mined), "out": str(out),
                      "size": out.stat().st_size, "merge": merge_report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
