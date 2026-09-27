"""Real entity ids for route templates — shared by the crawler and the passive runner.

`id_discovery` in .web-qa/config.json names one list endpoint per entity:

    [{"endpoint": "/orders", "key": "order"},
     {"endpoint": "/directory/contacts", "key": "contact", "route": "/people/{id}"}]

`discover_ids` fetches a sample id per entry; `materialize_path` puts the right one into
`/orders/{id}`. Lives apart from run_scenarios because explore needs it too, and
run_scenarios already imports explore.
"""

from __future__ import annotations

import re

import httpx

from route_mine import as_template

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def backend_client(cookies: dict, token: str | None) -> httpx.Client:
    """An httpx client that carries whatever the app's login handed back.

    Cookies alone were sent. For a JWT app the login returns a bearer token and sets no
    session cookie, so every backend probe a test case documents in its Steps answered 401 —
    and the passive runner reported that as the test case failing. The token was already in
    a local variable two frames up."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # follow_redirects: FastAPI answers `/orders/` with a 307 to `/orders`; a redirect is the
    # route working, and it was being reported as the test case failing
    return httpx.Client(cookies=cookies, headers=headers, timeout=10, follow_redirects=True)


def discover_ids(backend: str, cookies: dict, id_discovery: list[dict],
                 token: str | None = None) -> dict:
    """Fetch a sample entity id per config entry
    (config.json: [{"endpoint": "/orders", "key": "order"}, ...])."""
    out: dict = {}
    if not id_discovery:
        return out
    with backend_client(cookies, token) as cli:
        for spec in id_discovery:
            endpoint, key = spec["endpoint"], spec["key"]
            try:
                r = cli.get(f"{backend}{endpoint}")
                data = r.json()
                items = data if isinstance(data, list) else data.get("items") or data.get("results") or []
                if items and isinstance(items[0], dict):
                    out[key] = items[0].get("id") or items[0].get(f"{key}_id")
            except Exception:
                pass
    return out


def id_routes(id_discovery: list[dict] | None) -> dict[str, str]:
    """Frontend route → entity key, from the optional `route` of an id_discovery entry.

    Needed where the URL names nothing the API calls the entity: `/people/{id}` is a
    contact, and no rule can read that off the word «people»."""
    return {as_template(e["route"]): e["key"]
            for e in id_discovery or [] if e.get("route") and e.get("key")}


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"          # factories → factory, categories → category
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]                # orders → order, shipments → shipment
    return word


def materialize_path(path: str, ids: dict, routes: dict[str, str] | None = None) -> str:
    """Substitute {placeholder} tokens with ids discovered via id_discovery.

    Which entity a placeholder means is read from the path, most explicit first:
      1. the `route` of an id_discovery entry (`/people/{id}` → contact);
      2. the token itself: {order_id} / {order} → ids["order"];
      3. the segment in front of it: /shipments/{id} → ids["shipment"].
    Anything else stays a placeholder, and the caller skips that probe.

    It used to fall back to the FIRST discovered id for every generic {id}:
    /shipments/{id}/items opened with an order's id, and on a small dataset where
    the two id ranges overlapped, that even looked like it worked."""
    if "{" not in path:
        return path
    routes = routes or {}

    def repl(m: re.Match) -> str:
        token = m.group(1)
        prefix = path[:m.start()]
        candidates = [routes.get(as_template(prefix + "{id}"))]
        base = token[:-3] if token.endswith("_id") else token
        candidates += [base, token]
        segment = prefix.rstrip("/").rsplit("/", 1)[-1]
        if segment and not PLACEHOLDER.fullmatch(segment):
            candidates += [segment, _singular(segment)]
        for key in candidates:
            if key and ids.get(key):
                return str(ids[key])
        return m.group(0)

    return PLACEHOLDER.sub(repl, path)
