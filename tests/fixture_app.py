"""Tiny self-contained web app for integration tests — stdlib only.

Endpoints:
  POST /auth/login   → {access_token, email, role} (admin@example.com / viewer@example.com, pw "secret")
  GET  /health       → {"ok": true}
  GET  /openapi.json → minimal spec with a POST /items request schema
  POST /toggle-broken→ flips a layout banner on / (visual-regression fixture)
  GET  /             → dashboard (h1, nav, data-testid button, a DELIBERATE a11y violation)
  GET  /items        → table + form

Run standalone: python tests/fixture_app.py [port]
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USERS = {
    "admin@example.com": ("secret", "admin"),
    "viewer@example.com": ("secret", "viewer"),
}

INDEX_HTML = """<!doctype html><html lang="en"><head><title>Fixture</title></head>
<body>
<header><h1>Dashboard</h1></header>
{banner}
<nav aria-label="Main"><a href="/items">Items</a></nav>
<main>
  <p>Welcome to the fixture dashboard with three widgets.</p>
  <button data-testid="refresh-btn">Refresh data</button>
  <img src="logo.png">  <!-- deliberate a11y violation: missing alt -->
</main>
</body></html>"""

ITEMS_HTML = """<!doctype html><html lang="en"><head><title>Items</title></head>
<body>
<h1>Items</h1>
<nav aria-label="Main"><a href="/">Dashboard</a></nav>
<table><thead><tr><th>Name</th><th>Price</th></tr></thead>
<tbody><tr><td>Widget</td><td>10</td></tr><tr><td>Gadget</td><td>20</td></tr></tbody></table>
<form action="/items" method="post">
  <label for="n">Item name</label><input id="n" name="name" type="text">
  <button type="submit" data-testid="create-item">Create item</button>
</form>
</body></html>"""

_ITEM_SCHEMA = {
    "required": ["name"],
    "properties": {"name": {"type": "string"}, "price": {"type": "integer"}},
}
OPENAPI = {
    "paths": {
        "/items": {
            "get": {},
            "post": {"requestBody": {"content": {"application/json": {"schema": _ITEM_SCHEMA}}}},
        },
        "/auth/login": {"post": {}},
        "/health": {"get": {}},
    },
    "components": {"schemas": {}},
}


class Handler(BaseHTTPRequestHandler):
    broken = False

    def log_message(self, *a):  # silence
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/auth/login":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                body = {}
            u = USERS.get(body.get("email"))
            if u and u[0] == body.get("password"):
                self._json({"access_token": "tok-" + u[1], "email": body["email"], "role": u[1]})
            else:
                self._json({"detail": "bad credentials"}, 401)
        elif self.path == "/toggle-broken":
            type(self).broken = not type(self).broken
            self._json({"broken": type(self).broken})
        else:
            self._json({"detail": "not found"}, 404)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._json({"ok": True})
        elif path == "/openapi.json":
            self._json(OPENAPI)
        elif path == "/items":
            self._html(ITEMS_HTML)
        elif path == "/":
            banner = ('<div role="alert" style="background:#000;color:#fff;height:200px">'
                      'LAYOUT BROKEN BANNER</div>') if type(self).broken else ""
            self._html(INDEX_HTML.format(banner=banner))
        else:
            self._json({"detail": "not found"}, 404)


def start(port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


if __name__ == "__main__":
    import sys
    _, port = start(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(f"fixture app on http://127.0.0.1:{port}")
    threading.Event().wait()
