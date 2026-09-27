"""web-qa-doctor — one-command preflight.

Checks the environment and (with --alias) a specific project, printing a
✅/⚠️/❌ line per check with a fix hint. Exit codes: 0 = healthy (warnings
allowed), 1 = at least one hard failure.

Usage:
  doctor.py                      # environment only
  doctor.py --alias my-app       # environment + project
  doctor.py --alias my-app --json
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from registry import registry_path

SKILL = Path(__file__).resolve().parent.parent

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: "✅", WARN: "⚠️", FAIL: "❌"}


def check(results: list[dict], name: str, status: str, detail: str = "", hint: str = "") -> None:
    results.append({"name": name, "status": status, "detail": detail, "hint": hint})


MIN_TEST_TIMEOUT_MS = 45_000


def playwright_config_drift(text: str) -> list[str]:
    """Invariants a project's playwright.config.ts must hold, and which the template does.

    The template is copied ONCE, at project setup, and never again. Every fix landed in it
    since then is absent from every existing project, silently. Each of these was a real
    failure mode before it was an invariant."""
    import re
    problems: list[str] = []
    # Comments explain these invariants; they must not be mistaken for declaring them.
    code = re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", text), flags=re.S)
    # Specs navigate relative to baseURL and read WEBQA_BASE_URL through it; a literal host
    # here pins every spec to one stand, and the runners' WEBQA_BASE_URL is ignored.
    if "WEBQA_BASE_URL" not in code:
        problems.append("`baseURL` is hardcoded, not `process.env.WEBQA_BASE_URL ?? …`: the "
                        "suite runs only against that one host")
    if "WEBQA_LOCALE" not in code:
        problems.append("`locale` is not `process.env.WEBQA_LOCALE ?? …`: specs may see the app "
                        "in another language than the crawler that wrote their locators")
    if "actionTimeout" not in text:
        problems.append("no `actionTimeout`: a locator that never matches hangs until the "
                        "test timeout and reports no locator name")
    if "navigationTimeout" not in text:
        problems.append("no `navigationTimeout`: a hung goto burns the whole test budget")
    # Playwright deletes outputDir when it starts. One shared dir = every run destroys the
    # previous run's screenshots, traces and error-context page snapshots.
    if "WEBQA_OUTPUT_DIR" not in code:
        problems.append("`outputDir` is not driven by WEBQA_OUTPUT_DIR: every run deletes the "
                        "previous run's failure artifacts, and maintain heals from the wrong run")
    # A device descriptor carries its own viewport (Desktop Chrome → 1280x720) and silently
    # overrides a top-level `use.viewport`, because `projects[].use` wins. A top-level
    # viewport declared alongside ANY device spread therefore never applies — the specs run
    # at a size nobody chose. (A `mobile` project spreading a phone descriptor and keeping
    # that phone's viewport is correct and must not be flagged.)
    top = code.split("projects:", 1)[0]
    if "devices[" in code and re.search(r"\bviewport\s*:", top):
        problems.append("`use.viewport` is shadowed by the device descriptor in `projects` — "
                        "declare viewport inside `projects[].use`, after the spread")
    m = re.search(r"^\s*timeout:\s*(?:Number\([^)]*\?\?\s*)?([0-9_]+)", code, re.MULTILINE)
    if m and int(m.group(1).replace("_", "")) < MIN_TEST_TIMEOUT_MS:
        problems.append(f"test `timeout` is {m.group(1)} ms: a legitimately slow step (upload, "
                        f"async parse) is killed from the outside with a message naming nothing")
    return problems


# ---------- environment ----------

def check_environment(results: list[dict]) -> None:
    # python deps (we're running under the project env, so imports prove the sync)
    try:
        import playwright  # noqa: F401
        import httpx  # noqa: F401
        import PIL  # noqa: F401
        import numpy  # noqa: F401
        check(results, "python deps", OK, "playwright, httpx, pillow, numpy importable")
    except ImportError as e:
        check(results, "python deps", FAIL, str(e), "run: uv sync")

    # chromium: actually launch it headless — a path check lies when only the
    # headless shell (or only the full build) is present in the cache
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ver = browser.version
            browser.close()
        check(results, "chromium launch", OK, f"headless v{ver}")
    except Exception as e:
        check(results, "chromium launch", FAIL, str(e)[:140],
              "run: uv run playwright install chromium")

    # LLM CLI (generation/healing only — everything else works without it)
    llm = shutil.which("claude")
    if llm:
        check(results, "LLM CLI", OK, llm)
    else:
        check(results, "LLM CLI", WARN, "`claude` not on PATH",
              "spec-gen/generate/maintain need it; runs work without it")

    # node toolchain (spec running)
    if shutil.which("npx"):
        check(results, "node/npx", OK, shutil.which("npx"))
    else:
        check(results, "node/npx", WARN, "npx not on PATH",
              "needed for `npx playwright test` (specs stage)")

    # registry (env override → skill root → ~/.config/web-qa for plugin installs)
    reg = registry_path()
    if not reg.is_file():
        check(results, "projects.json", FAIL, f"missing at {reg}",
              "run bin/web-qa-register-project <alias> --target-url <url>")
        return
    try:
        entries = json.loads(reg.read_text())
        aliases = [e.get("alias") for e in entries if e.get("path") != "*"]
        check(results, "projects.json", OK, f"{len(aliases)} project(s): {', '.join(map(str, aliases)) or '—'}")
    except json.JSONDecodeError as e:
        check(results, "projects.json", FAIL, f"invalid JSON: {e}", "fix the syntax")


# ---------- project ----------

def http_ok(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        import httpx
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        return r.status_code < 500, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def check_project(results: list[dict], alias: str) -> None:
    # imported lazily so a broken env fails in the deps CHECK, not with a traceback on startup
    from explore import (api_login, browser_locale, load_project, registry_secrets,
                         resolve_credentials, viewport_entries)
    try:
        proj = load_project(alias)
    except SystemExit as e:
        check(results, "project", FAIL, str(e), "web-qa-register-project or fix .web-qa/config.json")
        return
    check(results, "project", OK, f"{alias} → {proj.get('path')}")

    path = Path(proj.get("path", ""))
    if not path.is_dir():
        check(results, "project path", FAIL, f"{path} does not exist", "fix `path` in projects.json")
        return
    webqa = path / ".web-qa"
    if not webqa.is_dir():
        check(results, ".web-qa dir", FAIL, f"missing at {webqa}", "run web-qa-register-project")
        return
    check(results, ".web-qa dir", OK, str(webqa))

    entries = viewport_entries(proj)
    desc = ", ".join((e.get("device") or f"{e.get('width')}x{e.get('height')}")
                     + (" (default)" if i == 0 and len(entries) > 1 else "")
                     for i, e in enumerate(entries))
    configured = bool(proj.get("viewport") or proj.get("viewports"))
    check(results, "viewport", OK, desc + ("" if configured else " (default)"))

    # device viewports may need a non-chromium engine (iPhone → webkit) that isn't
    # installed — the specs stage then dies with "Executable doesn't exist"
    device_names = [e["device"] for e in entries if e.get("device")]
    if device_names:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                for name in device_names:
                    d = p.devices.get(name)
                    if not d:
                        check(results, f"device: {name}", FAIL, "unknown Playwright device descriptor",
                              "pick an exact name from Playwright's device list")
                        continue
                    engine = d.get("default_browser_type", "chromium")
                    if engine == "chromium":
                        check(results, f"device: {name}", OK, "chromium engine")
                        continue
                    exe = Path(getattr(p, engine).executable_path)
                    if exe.exists():
                        check(results, f"device: {name}", OK, f"{engine} installed")
                    else:
                        check(results, f"device: {name}", FAIL, f"needs {engine}, which is not installed",
                              f"uv run playwright install {engine} — or use a chromium device (e.g. Pixel 7)")
        except Exception as e:
            check(results, "device engines", WARN, f"could not verify: {str(e)[:80]}")

    # reachability
    target = proj.get("target_url")
    if not target:
        check(results, "target_url", FAIL, "not set", "set target_url in projects.json")
        return
    ok, detail = http_ok(target)
    check(results, "frontend reachable", OK if ok else FAIL, f"{target} → {detail}",
          "" if ok else "start the dev server")

    backend = proj.get("backend_url")
    if backend:
        ok_b = False
        for probe in ("/health", "/openapi.json", "/"):
            ok_b, detail_b = http_ok(backend.rstrip("/") + probe)
            if ok_b:
                check(results, "backend reachable", OK, f"{backend}{probe} → {detail_b}")
                break
        if not ok_b:
            check(results, "backend reachable", FAIL, f"{backend} → {detail_b}", "start the backend")

    # credentials
    try:
        email, password = resolve_credentials(proj, None, None)
        try:
            _, me, _tok = api_login(backend or target, email, password, proj)
            check(results, "login", OK, f"{me.get('email', email)} ({me.get('role', '?')})")
        except Exception as e:
            check(results, "login", FAIL, f"{type(e).__name__}: {str(e)[:100]}",
                  "check auth in projects.json and the login endpoint (auth_login_path)")
    except SystemExit as e:
        check(results, "credentials", FAIL, str(e)[:120], "set auth in projects.json")

    for r in proj.get("roles") or []:
        name = r.get("name", "?")
        try:
            _, me, _ = api_login(backend or target, r.get("email", ""), r.get("password", ""), proj)
            actual = me.get("role")
            if actual is None:
                # A 2xx from /auth/login proves the password, never the privileges. RBAC test
                # cases run under this account and their whole verdict rests on its role.
                check(results, f"role: {name}", WARN, f"{r.get('email','')} — login OK, role not readable",
                      "set `auth_me_path` in .web-qa/config.json so the role can be verified")
            elif str(actual).lower() != name.lower():
                check(results, f"role: {name}", FAIL, f"{r.get('email','')} actually has role {actual!r}",
                      "RBAC test cases under this role would assert against the wrong privileges")
            else:
                check(results, f"role: {name}", OK, f"{r.get('email','')} → {actual}")
        except Exception as e:
            check(results, f"role: {name}", FAIL, f"{type(e).__name__}: {str(e)[:80]}",
                  "fix this role's credentials in projects.json")

    # per-project artifacts
    ctx = webqa / "app.context.md"
    check(results, "app.context.md", OK if ctx.is_file() else WARN,
          f"{ctx.stat().st_size} bytes" if ctx.is_file() else "missing",
          "" if ctx.is_file() else "run web-qa-explore (spec-gen quality depends on it)")

    sidecar = webqa / "app.context.json"
    if sidecar.is_file():
        try:
            mapped = json.loads(sidecar.read_text(encoding="utf-8")).get("locale")
        except (OSError, json.JSONDecodeError):
            mapped = None
        wanted = browser_locale(proj)
        if mapped != wanted:
            check(results, "map locale", WARN,
                  f"map crawled with locale {mapped or 'unset'}, project runs {wanted}",
                  "re-run web-qa-explore: an app that picks its language from the browser "
                  "shows specs a UI their locators were not written for")
        else:
            check(results, "map locale", OK, wanted)

    scen = list((webqa / "scenarios").glob("*.md")) if (webqa / "scenarios").is_dir() else []
    check(results, "scenarios", OK if scen else WARN, f"{len(scen)} file(s)",
          "" if scen else "web-qa-generate --diff/--task, or write scenarios/*.md")

    # specs generated before spec_env carry the registry's passwords in their source
    secrets = registry_secrets(proj)
    spec_files = sorted((webqa / "specs").glob("*.spec.ts")) if (webqa / "specs").is_dir() else []
    leaking = [f.name for f in spec_files
               if any(pw in f.read_text(encoding="utf-8", errors="ignore") for pw in secrets)]
    if leaking:
        check(results, "specs: secrets", WARN,
              f"{len(leaking)} spec(s) contain a registry password in plain text, e.g. {leaking[0]}",
              "regenerate them (web-qa-spec-gen --force) — generated specs now read logins from "
              "WEBQA_* env vars; and check whether any was committed")
    elif spec_files:
        check(results, "specs: secrets", OK, "no registry password in any spec")

    # specs runner setup
    pw_config = webqa / "playwright.config.ts"
    if pw_config.is_file() and (webqa / "node_modules" / "@playwright" / "test").is_dir():
        check(results, "specs runner", OK, "playwright.config.ts + @playwright/test present")
        drift = playwright_config_drift(pw_config.read_text(encoding="utf-8"))
        if drift:
            check(results, "playwright.config.ts", WARN, f"{len(drift)} problem(s): " + "; ".join(drift),
                  f"re-copy {SKILL / 'playwright.config.template.ts'} (the stand's URL now "
                  f"comes from the registry via WEBQA_BASE_URL)")
        else:
            check(results, "playwright.config.ts", OK, "matches the template's invariants")
    else:
        check(results, "specs runner", WARN, "not set up",
          "see SKILL.md 'Per-project specs runner setup' (needed for specs/matrix specs stage)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", help="also check this project")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results: list[dict] = []
    check_environment(results)
    if args.alias:
        check_project(results, args.alias)

    failed = any(r["status"] == FAIL for r in results)
    if args.json:
        print(json.dumps({"healthy": not failed, "checks": results}, ensure_ascii=False, indent=2))
    else:
        width = max(len(r["name"]) for r in results)
        for r in results:
            line = f"{ICON[r['status']]} {r['name']:<{width}}  {r['detail']}"
            if r["hint"] and r["status"] != OK:
                line += f"\n   ↳ {r['hint']}"
            print(line)
        print(f"\n{'❌ problems found' if failed else '✅ healthy'}"
              + ("" if args.alias else " (environment only — add --alias <a> for project checks)"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
