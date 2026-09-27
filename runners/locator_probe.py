"""Live locator probe — validate a generated spec's locators against the RUNNING app
before the spec is accepted.

`playwright test --list` only proves the file parses; whether `getByRole('button',
{name: 'Publish'})` matches anything is only discoverable against the live DOM. Without
this check a selector miss surfaces at run time and costs a full generate→run→maintain
loop. The probe opens the spec's entry page (authenticated via the same auth adapter as
the crawler) and counts matches for every STATIC locator literal:

  0 matches  → miss, fed back into the generation retry prompt
  >1 matches → strict-mode ambiguity (Playwright will refuse to act) — also fed back

Honest limits: only the entry page is probed — elements that appear mid-flow can't be
checked without executing the flow (that's what `run` is for). Template literals,
variables and regex names are skipped. A miss is a strong hint, not proof: the retry
prompt tells the model to keep locators that only appear after an interaction.
"""

from __future__ import annotations

import re
from pathlib import Path

RE_LOCATOR = re.compile(
    r"\.getBy(Role|Label|TestId|Placeholder|Text)\(\s*(['\"])((?:(?!\2).)*)\2\s*(?:,\s*\{([^}]*)\})?\s*\)")
RE_NAME_LITERAL = re.compile(r"name:\s*(['\"])((?:(?!\1).)*)\1")
RE_NAME_OPTION = re.compile(r"\bname\s*:")
RE_GOTO = re.compile(r"\.goto\(([^)]*)\)")
RE_PATH_LITERAL = re.compile(r"['\"](/[^'\"]*)['\"]")


def extract_locators(source: str) -> list[dict]:
    """Static locator literals from a .spec.ts. Skips template values and regex/variable
    names — only what can be resolved without executing the test."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for m in RE_LOCATOR.finditer(source):
        kind, value, opts = m.group(1), m.group(3), m.group(4) or ""
        if "${" in value:
            continue
        name = None
        if RE_NAME_OPTION.search(opts):
            nm = RE_NAME_LITERAL.search(opts)
            if not nm or "${" in nm.group(2):
                continue  # regex or variable name — not statically resolvable
            name = nm.group(2)
        key = (kind, value, name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "value": value, "name": name,
                    "raw": m.group(0).lstrip(".")})
    return out


# The first interaction after the first goto ends the page's ENTRY state. Anything located
# after it — a dialog's fields, a menu's items, a row the spec just created — does not exist
# yet when the probe looks. 34 of 35 probe retries in one run "fixed" such locators into
# the same correct code, each at the price of a full model call.
RE_ACTION = re.compile(
    r"\.(?:click|dblclick|fill|press|type|check|uncheck|selectOption|setInputFiles|dragTo|hover|tap)\(")
# Roles that exist only after something opened them, wherever the spec mentions them.
TRANSIENT_ROLES = {"dialog", "alertdialog", "menu", "menuitem", "listbox", "option", "tooltip"}

def entry_locators(source: str) -> list[dict]:
    """The static locators the entry page itself must have: those up to the first action
    after the first `goto` (inclusive) or the next navigation, whichever comes first, minus
    roles that only appear once opened."""
    goto = RE_GOTO.search(source)
    if goto:
        # the entry state also ends where the spec navigates on: a second goto, a waitForURL
        ends = [m.end() for m in (RE_ACTION.search(source, goto.end()),) if m]
        ends += [m.start() for m in (RE_GOTO.search(source, goto.end()),
                                     re.compile(r"\.waitForURL\(").search(source, goto.end())) if m]
        if ends:
            source = source[:min(ends)]
    return [loc for loc in extract_locators(source)
            if not (loc["kind"] == "Role" and loc["value"] in TRANSIENT_ROLES)]

def entry_path(source: str) -> str | None:
    """Path of the FIRST page.goto — the page the flow starts on. None when it is
    dynamic (template slug etc.): probing a guessed page would produce false misses."""
    m = RE_GOTO.search(source)
    if not m:
        return None
    arg = m.group(1)
    if "${" in arg:
        return None
    pm = RE_PATH_LITERAL.search(arg)
    return pm.group(1) if pm else None


def classify_count(loc: dict, n: int) -> str | None:
    """'miss' | 'ambiguous' | None (ok). A nameless getByRole legitimately matches many
    elements (it's usually narrowed later in the chain), so only named/uniquely-typed
    locators count as strict-mode ambiguity."""
    if n == 0:
        return "miss"
    if n > 1 and (loc["name"] is not None or loc["kind"] != "Role"):
        return "ambiguous"
    return None


def _resolve(page, loc: dict):
    kind, value, name = loc["kind"], loc["value"], loc["name"]
    if kind == "Role":
        return page.get_by_role(value, name=name) if name else page.get_by_role(value)
    if kind == "Label":
        return page.get_by_label(value)
    if kind == "TestId":
        return page.get_by_test_id(value)
    if kind == "Placeholder":
        return page.get_by_placeholder(value)
    if kind == "Text":
        return page.get_by_text(value)
    return None


RE_EMAIL_LITERAL = re.compile(r"['\"]([^'\"\s]+@[^'\"\s]+)['\"]")


def role_from_spec(source: str, proj: dict) -> str | None:
    """Infer which ROLE the spec browses as, by matching email literals in its source
    against the registry's `roles`. RBAC matters here: probing an admin-only element
    under a reader session yields a false zero. A spec may contain several accounts
    (API setup as one, UI as another) — a role-account match wins over the default
    `auth` account, because role creds only appear when the TC declared that role."""
    from explore import credential_vars
    emails = set(RE_EMAIL_LITERAL.findall(source))
    for r in proj.get("roles") or []:
        if r.get("email") in emails:
            return r.get("name")
        # current specs carry no literals — they read `process.env.WEBQA_ROLE_<NAME>_EMAIL`
        if r.get("name") and credential_vars(r["name"])[0] in source:
            return r.get("name")
    return None


def probe_spec(spec_path: Path, proj: dict, role: str | None = None) -> dict | None:
    """None = probe not applicable (dynamic entry URL, no locators, no creds, stand
    unreachable) — never blocks generation, absence of evidence is not a failure.
    `role` selects the login account; without it the spec's own email literals decide
    (see role_from_spec) so the probe sees the SAME page the spec's session would."""
    source = spec_path.read_text(encoding="utf-8")
    path = entry_path(source)
    locators = entry_locators(source)
    if not path or not locators:
        return None
    from explore import api_login, build_storage_state, context_kwargs_for, resolve_credentials, viewport_entry
    try:
        email, password = resolve_credentials(proj, None, None, role or role_from_spec(source, proj))
        cookies, _me, token = api_login(proj.get("backend_url") or proj["target_url"],
                                        email, password, proj)
        storage = build_storage_state(cookies, proj["target_url"], token, proj)
    except (SystemExit, Exception):
        return None

    result = {"url": path, "checked": 0, "misses": [], "ambiguous": [], "skipped": 0}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(storage_state=storage,
                                      **context_kwargs_for(viewport_entry(proj), p))
            page = ctx.new_page()
            page.goto(proj["target_url"].rstrip("/") + path,
                      wait_until="domcontentloaded", timeout=15000)
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass  # SPA with polling — bounded wait is enough
            for loc in locators:
                locator = _resolve(page, loc)
                if locator is None:
                    result["skipped"] += 1
                    continue
                result["checked"] += 1
                verdict = classify_count(loc, locator.count())
                if verdict == "miss":
                    result["misses"].append(loc["raw"])
                elif verdict == "ambiguous":
                    result["ambiguous"].append(f"{loc['raw']} → {locator.count()} elements")
            browser.close()
    except Exception:
        return None
    return result


def probe_feedback(result: dict | None) -> str | None:
    """Human/LLM-readable summary of probe problems; None = nothing to report."""
    if not result or not (result["misses"] or result["ambiguous"]):
        return None
    lines: list[str] = []
    if result["misses"]:
        lines.append(f"resolved to 0 elements on `{result['url']}`:")
        lines += [f"  - {m}" for m in result["misses"]]
    if result["ambiguous"]:
        lines.append(f"match SEVERAL elements on `{result['url']}` "
                     "(strict mode will refuse to act — disambiguate or .first()):")
        lines += [f"  - {a}" for a in result["ambiguous"]]
    return "\n".join(lines)
