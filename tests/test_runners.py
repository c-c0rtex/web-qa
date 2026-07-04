"""Unit tests for the pure logic of the runners — no browser, no network, no LLM."""

import json

import pytest

from explore import (
    context_kwargs_for,
    merge_manual_section,
    normalize_for_dedup,
    project_viewport,
    render_context_md,
    resolve_credentials,
    viewport_entries,
    viewport_entry,
    viewport_env,
    viewport_suffix,
)
from matrix import (
    collect_specs,
    compute_coverage,
    coverage_by_role,
    routes_from_context,
    row_key,
    rows_for_role,
    update_history,
)
from maintain import classify_heal_output, failing_specs_from_report, record_app_bug
from spec_gen import load_seed
from run_scenarios import (
    classify,
    extract_paths,
    infer_root_path,
    materialize_path,
    split_tcs,
    tc_roles,
    visual_diff_pct,
)
from spec_gen import PROMPT_TEMPLATE, postprocess_spec, slugify


# ---------- explore ----------

def test_normalize_for_dedup_collapses_ids_and_query():
    assert normalize_for_dedup("http://x/orders?page=2#top") == "http://x/orders"
    assert normalize_for_dedup("http://x/orders/17") == "http://x/orders/{id}"
    assert normalize_for_dedup("http://x/orders/17/items/3") == "http://x/orders/{id}/items/{id}"
    assert normalize_for_dedup("http://x/orders/17") == normalize_for_dedup("http://x/orders/42")


def test_resolve_credentials_priority_and_roles():
    proj = {
        "alias": "x",
        "auth": {"email": "a@a", "password": "p1"},
        "roles": [{"name": "manager", "email": "m@m", "password": "p2"}],
    }
    assert resolve_credentials(proj, None, None) == ("a@a", "p1")
    assert resolve_credentials(proj, None, None, role="manager") == ("m@m", "p2")
    assert resolve_credentials(proj, "cli@cli", "pw", role="manager") == ("cli@cli", "pw")
    with pytest.raises(SystemExit, match="ghost"):
        resolve_credentials(proj, None, None, role="ghost")
    with pytest.raises(SystemExit, match="no credentials"):
        resolve_credentials({"alias": "y", "auth": {}}, None, None)


def test_render_context_md_includes_openapi_request_schemas():
    openapi = {
        "paths": {"/orders": {"get": {}, "post": {"requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/OrderIn"}}}}}}},
        "components": {"schemas": {"OrderIn": {
            "required": ["model_name"],
            "properties": {"model_name": {"type": "string"}, "quantity": {"type": "integer"}},
        }}},
    }
    md = render_context_md({"alias": "t", "target_url": "http://x"}, [], openapi, {})
    assert "POST body: model_name*:string, quantity:integer" in md


def test_merge_manual_section_scaffolds_marker_on_first_write():
    out = merge_manual_section("# map\ncontent", None)
    assert out.startswith("# map\ncontent\n\n<!-- manual -->")
    assert "survives" in out


def test_merge_manual_section_preserves_hand_written_notes():
    existing = "# old map\nstale\n\n<!-- manual -->\n## Business rules\n- only admins delete\n"
    out = merge_manual_section("# new map\nfresh", existing)
    assert "stale" not in out
    assert "fresh" in out
    assert "- only admins delete" in out
    # idempotent: a second re-crawl keeps the same manual tail
    again = merge_manual_section("# newer map", out)
    assert again.count("<!-- manual -->") == 1
    assert "- only admins delete" in again


def test_project_viewport_and_env():
    assert project_viewport({}) == {"width": 1280, "height": 900}
    assert project_viewport({"viewport": {"width": 390, "height": 844}}) == {"width": 390, "height": 844}
    assert viewport_env({}) is None
    assert viewport_env({"viewport": {"width": 390, "height": 844}}) == "390x844"


VP_PROJ = {"viewports": [
    {"name": "desktop", "width": 1280, "height": 900},
    {"name": "mobile", "device": "iPhone 14"},
]}


def test_viewport_entries_fallbacks_and_lookup():
    # no config at all → single default entry
    assert viewport_entries({}) == [{"name": "default", "width": 1280, "height": 900}]
    # v0.2 single `viewport` key still honored
    assert viewport_entries({"viewport": {"width": 800, "height": 600}})[0]["width"] == 800
    # named lookup + first-is-default
    assert viewport_entry(VP_PROJ)["name"] == "desktop"
    assert viewport_entry(VP_PROJ, "mobile")["device"] == "iPhone 14"
    with pytest.raises(SystemExit, match="ghost"):
        viewport_entry(VP_PROJ, "ghost")


def test_viewport_suffix_default_unsuffixed():
    assert viewport_suffix(VP_PROJ, None) == ""
    assert viewport_suffix(VP_PROJ, "desktop") == ""  # project default keeps old baseline names
    assert viewport_suffix(VP_PROJ, "mobile") == "@mobile"


class _FakePlaywright:
    devices = {"iPhone 14": {"viewport": {"width": 390, "height": 664}, "is_mobile": True,
                             "has_touch": True, "user_agent": "Mobile Safari"}}


def test_context_kwargs_device_vs_size():
    kw = context_kwargs_for({"name": "mobile", "device": "iPhone 14"}, _FakePlaywright())
    assert kw["is_mobile"] and kw["has_touch"] and "Mobile" in kw["user_agent"]
    kw = context_kwargs_for({"name": "desktop", "width": 800, "height": 600}, _FakePlaywright())
    assert kw == {"viewport": {"width": 800, "height": 600}}
    with pytest.raises(SystemExit, match="unknown Playwright device"):
        context_kwargs_for({"device": "Nokia 3310"}, _FakePlaywright())


# ---------- run_scenarios ----------

BACKEND_PREFIXES = ("/auth", "/orders/facets", "/orders/{")


def test_extract_paths_separates_frontend_and_backend():
    fronts, backs = extract_paths("Open `/orders`, then GET `/orders/facets`", BACKEND_PREFIXES)
    assert fronts == ["/orders"]
    assert backs == [("GET", "/orders/facets")]


def test_classify_mutating_by_method_and_prose():
    kind, reasons = classify("call POST /orders", [("POST", "/orders")])
    assert kind == "mutating" and reasons
    kind, _ = classify("just look at the table", [("GET", "/orders")])
    assert kind == "passive"


def test_infer_root_path_uses_config_hints_only():
    tc = {"title": "order status filter", "body": ""}
    hints = [{"path": "/orders", "keywords": ["order"]}]
    assert infer_root_path(tc, hints) == "/orders"
    assert infer_root_path(tc, []) is None


def test_materialize_path_is_data_driven():
    ids = {"order": 42, "shipment": 7, "user": 3}
    assert materialize_path("/orders/{id}", ids) == "/orders/42"
    assert materialize_path("/orders/{order_id}", ids) == "/orders/42"
    assert materialize_path("/shipments/{shipment_id}/packing", ids) == "/shipments/7/packing"
    assert materialize_path("/x/{unknown}", ids) == "/x/{unknown}"


def test_split_tcs_parses_headers():
    md = "## TC-A1 — first case\nbody a\n## TC-B2 — second\nbody b\n"
    tcs = split_tcs(md)
    assert [t["id"] for t in tcs] == ["TC-A1", "TC-B2"]
    assert tcs[0]["title"] == "first case"
    assert "body a" in tcs[0]["body"]


def test_tc_roles_parsing():
    assert tc_roles("**Type:** passive\n**Role:** viewer\n**Steps:**") == ["viewer"]
    assert tc_roles("**Roles:** admin, `editor`\nbody") == ["admin", "editor"]
    assert tc_roles("**Role:** Viewer") == ["viewer"]  # normalized to lowercase
    assert tc_roles("no role here") == []


def test_rows_for_role_targets_declared_tcs():
    rows = [
        {"id": "TC-1", "roles": []},              # role-agnostic → every combo
        {"id": "TC-2", "roles": ["viewer"]},      # only the viewer combo
        {"id": "TC-3", "roles": ["admin", "viewer"]},
    ]
    assert [r["id"] for r in rows_for_role(rows, "viewer")] == ["TC-1", "TC-2", "TC-3"]
    assert [r["id"] for r in rows_for_role(rows, "admin")] == ["TC-1", "TC-3"]
    assert [r["id"] for r in rows_for_role(rows, "Viewer")] == ["TC-1", "TC-2", "TC-3"]
    # no --roles → nothing dropped (the runner marks declared TCs as skip with a hint)
    assert len(rows_for_role(rows, None)) == 3


def test_visual_diff_size_mismatch_is_flagged(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    PIL.new("RGB", (50, 50), "white").save(tmp_path / "b.png")
    PIL.new("RGB", (50, 60), "white").save(tmp_path / "c.png")
    PIL.new("RGB", (50, 50), "white").save(tmp_path / "same.png")
    assert visual_diff_pct(tmp_path / "b.png", tmp_path / "c.png") == "size-mismatch"
    assert visual_diff_pct(tmp_path / "b.png", tmp_path / "same.png") == 0.0


# ---------- spec_gen ----------

def test_postprocess_downgrades_networkidle():
    assert postprocess_spec("waitForLoadState('networkidle')") == "waitForLoadState('domcontentloaded')"
    assert "networkidle" not in postprocess_spec('goto("/x", { waitUntil: "networkidle" })')


def test_prompt_format_survives_braces_in_values():
    p = PROMPT_TEMPLATE.format(
        stack="s", frontend_url="f", backend_url="b", login_email="e", login_password="p",
        test_data_prefix="QA-",
        auth_login_hint="use `Bearer ${access_token}` and {email, password}",
        seed_section="```ts\nconst t = `x${y}`;\n```",
        app_context="ctx", tc_body="## TC-1 — t",
    )
    assert "Bearer ${access_token}" in p and "{email, password}" in p


def test_slugify():
    assert slugify("TC-I4 — Проверка заказа!") .startswith("tc-i4")
    assert "/" not in slugify("a/b/c")


# ---------- matrix ----------

def _webqa(tmp_path):
    (tmp_path / "specs").mkdir()
    return tmp_path


def test_collect_specs_adhoc_and_gate_exclude(tmp_path):
    d = _webqa(tmp_path)
    for n in ("analytics-x.spec.ts", "orders.spec.ts", "_debug.spec.ts"):
        (d / "specs" / n).write_text("")
    rows, excluded = collect_specs(d, include_adhoc=False, exclude_globs=["analytics*"])
    assert [r["file"] for r in rows] == ["orders.spec.ts"]
    assert excluded == ["analytics-x.spec.ts"]
    rows, _ = collect_specs(d, include_adhoc=True, exclude_globs=[])
    assert len(rows) == 3


def test_route_coverage_with_id_templates(tmp_path):
    d = _webqa(tmp_path)
    (d / "app.context.md").write_text(
        "| `/orders` | t | h | 1 | 1 | 5 |\n"
        "| `/orders/235` | t | h | 0 | 1 | 3 |\n"
        "| `/import` | t | h | 1 | 0 | 2 |\n"
    )
    assert routes_from_context(d) == ["/orders", "/orders/{id}", "/import"]
    (d / "specs" / "s1.spec.ts").write_text("await page.goto('/orders/42');")
    scen = [{"source": "scenario", "file": "f.md", "id": "TC-1", "paths": ["/orders"],
             "role": "-", "status": "pass", "kind": "passive", "title": "t"}]
    specs = [{"source": "spec", "file": "s1.spec.ts", "id": "", "role": "-",
              "status": "pass", "kind": "spec", "title": "s1"}]
    cov = compute_coverage(d, scen, specs)
    assert cov["covered"] == 2 and cov["uncovered"] == ["/import"]


def test_coverage_by_role_finds_role_gaps(tmp_path):
    d = _webqa(tmp_path)
    (d / "app.context.md").write_text("| `/orders` | t | h | 1 | 1 | 5 |\n| `/import` | t | h | 1 | 0 | 2 |\n")
    scen = [
        {"source": "scenario", "file": "f.md", "id": "TC-1", "paths": ["/orders", "/import"],
         "role": "admin", "viewport": "-", "status": "pass", "kind": "passive", "title": "t"},
        {"source": "scenario", "file": "f.md", "id": "TC-1", "paths": ["/orders"],
         "role": "viewer", "viewport": "-", "status": "pass", "kind": "passive", "title": "t"},
    ]
    rc = coverage_by_role(d, scen)
    assert rc["admin"]["uncovered"] == []
    assert rc["viewer"]["uncovered"] == ["/import"]
    # single default role → empty dict, section stays hidden
    assert coverage_by_role(d, [{"role": "-", "paths": [], "source": "s", "file": "f", "id": "x"}]) == {}


def test_row_key_separates_role_and_viewport():
    base = {"source": "scenario", "file": "f.md", "id": "TC-1"}
    k1 = row_key({**base, "role": "admin", "viewport": "mobile"})
    k2 = row_key({**base, "role": "admin", "viewport": "-"})
    k3 = row_key({**base, "role": "viewer", "viewport": "mobile"})
    assert len({k1, k2, k3}) == 3


def test_history_flags_flaky(tmp_path):
    d = _webqa(tmp_path)
    row = {"source": "scenario", "file": "f.md", "id": "TC-1", "role": "-",
           "status": "pass", "kind": "passive", "title": "t"}
    for status in ("pass", "fail", "pass"):
        row["status"] = status
        flaky = update_history(d, f"run-{status}", [row])
    assert row_key(row) in flaky
    assert len(json.loads((d / "history.json").read_text())) == 3


# ---------- gen_scenarios ----------

def test_rbac_trigger_ignores_aria_and_table_markup():
    from gen_scenarios import RE_RBAC_CONTENT, RE_RBAC_FILES
    aria_diff = '+  <div role="dialog" aria-modal="true">\n+  <th scope="col">Name</th>'
    assert not RE_RBAC_CONTENT.search(aria_diff)
    assert not RE_RBAC_FILES.search(aria_diff)


def test_rbac_trigger_fires_on_real_access_control():
    from gen_scenarios import RE_RBAC_CONTENT, RE_RBAC_FILES
    assert RE_RBAC_CONTENT.search("+  if (!hasPermission(user, 'orders', 'edit')) return null;")
    assert RE_RBAC_CONTENT.search("+    require_scope_for_method(scope='orders')")
    assert RE_RBAC_CONTENT.search("changed the permission model for editors")
    stat = "Files changed:\n app/backend/app/roles.py        | 24 ++++---\n app/frontend/lib/auth.ts        |  8 +-"
    assert RE_RBAC_FILES.search(stat)
    assert not RE_RBAC_FILES.search(" app/frontend/components/table.tsx | 5 +--")


# ---------- maintain ----------

def test_classify_heal_output_three_way():
    assert classify_heal_output("// TRANSIENT: dev server was down\nimport ...") == \
        ("transient", "dev server was down")
    kind, detail = classify_heal_output("// APP-BUG: needs_sale_price missing from presenter\ntest.fixme(...)")
    assert kind == "app-bug" and "presenter" in detail
    assert classify_heal_output("import { test } from '@playwright/test';") == ("fix", "")
    # marker must be at the head, not buried in code
    assert classify_heal_output("import x;\n" + "a\n" * 40 + "// APP-BUG: deep")[0] == "fix"


def test_record_app_bug_appends(tmp_path):
    (tmp_path / ".web-qa").mkdir()
    record_app_bug(tmp_path, "orders.spec.ts", "delete button 500s")
    record_app_bug(tmp_path, "cart.spec.ts", "total ignores discount")
    text = (tmp_path / ".web-qa" / "BUGS.md").read_text()
    assert text.startswith("# BUGS")
    assert "orders.spec.ts" in text and "total ignores discount" in text


def test_load_seed_absent_and_present(tmp_path):
    (tmp_path / ".web-qa").mkdir()
    assert load_seed(tmp_path) == ""
    (tmp_path / ".web-qa" / "seed.spec.ts").write_text("const AUTH = 'known-good';")
    assert "known-good" in load_seed(tmp_path)


def test_render_context_md_includes_aria_section():
    pages = [{"path": "/orders", "title": "Orders", "headings": [], "forms": [], "tables": [],
              "buttons": [], "links": [], "aria": "- button \"Create order\"\n- table"}]
    md = render_context_md({"alias": "t", "target_url": "http://x"}, pages, {}, {})
    assert "ARIA snapshots" in md
    assert 'button "Create order"' in md


def test_failing_specs_extracted_with_errors():
    report = {"suites": [{"suites": [{"specs": [
        {"file": "specs/a.spec.ts", "tests": [{"status": "unexpected", "results": [
            {"error": {"message": "getByRole('button', { name: 'Save' }) not found"}}]}]},
        {"file": "specs/b.spec.ts", "tests": [{"status": "expected", "results": []}]},
    ]}]}]}
    fails = failing_specs_from_report(report)
    assert list(fails) == ["a.spec.ts"]
    assert "Save" in fails["a.spec.ts"][0]


def test_mobile_device_found_after_sizeonly_viewport():
    # regression: next() must skip size-only entries, not return their None
    vps = ["desktop", "mobile"]
    dev = next((d for v in vps if v for d in [viewport_entry(VP_PROJ, v).get("device")] if d), None)
    assert dev == "iPhone 14"


def test_role_names_case_insensitive():
    proj = {"alias": "x", "auth": {}, "roles": [{"name": "Viewer", "email": "v@v", "password": "p"}]}
    assert resolve_credentials(proj, None, None, role="viewer") == ("v@v", "p")


def test_history_migrates_legacy_keys(tmp_path):
    d = tmp_path
    (d / "history.json").write_text(json.dumps(
        [{"run_id": "old", "statuses": {"scenario:f.md:TC-1:-": "fail"}}]))
    row = {"source": "scenario", "file": "f.md", "id": "TC-1", "role": "-", "viewport": "-",
           "status": "pass", "kind": "passive", "title": "t"}
    flaky = update_history(d, "new", [row])
    assert row_key(row) in flaky  # old fail + new pass across formats = flaky
