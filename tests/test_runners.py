"""Unit tests for the pure logic of the runners — no browser, no network, no LLM."""

import json

import pytest

from explore import normalize_for_dedup, render_context_md, resolve_credentials
from matrix import collect_specs, compute_coverage, routes_from_context, row_key, update_history
from maintain import failing_specs_from_report
from run_scenarios import classify, extract_paths, infer_root_path, materialize_path, split_tcs, visual_diff_pct
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


def test_history_flags_flaky(tmp_path):
    d = _webqa(tmp_path)
    row = {"source": "scenario", "file": "f.md", "id": "TC-1", "role": "-",
           "status": "pass", "kind": "passive", "title": "t"}
    for status in ("pass", "fail", "pass"):
        row["status"] = status
        flaky = update_history(d, f"run-{status}", [row])
    assert row_key(row) in flaky
    assert len(json.loads((d / "history.json").read_text())) == 3


# ---------- maintain ----------

def test_failing_specs_extracted_with_errors():
    report = {"suites": [{"suites": [{"specs": [
        {"file": "specs/a.spec.ts", "tests": [{"status": "unexpected", "results": [
            {"error": {"message": "getByRole('button', { name: 'Save' }) not found"}}]}]},
        {"file": "specs/b.spec.ts", "tests": [{"status": "expected", "results": []}]},
    ]}]}]}
    fails = failing_specs_from_report(report)
    assert list(fails) == ["a.spec.ts"]
    assert "Save" in fails["a.spec.ts"][0]
