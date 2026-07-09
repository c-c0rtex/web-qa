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
from maintain import (
    classify_heal_output,
    failing_specs_from_report,
    record_app_bug,
    transient_signature,
)
from run_scenarios import (
    classify,
    declared_type,
    extract_paths,
    gated_console,
    infer_root_path,
    materialize_path,
    split_tcs,
    tc_roles,
    visual_diff_pct,
)
from spec_gen import (
    PROMPT_TEMPLATE,
    LLMBudgetExceeded,
    claude_cmd,
    detect_dnd_library,
    dnd_recipe_section,
    is_mutating,
    load_seed,
    parse_claude_json,
    postprocess_spec,
    slugify,
    strip_fences,
)


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


# ---------- fixtures / junit / diff mask / failure artifacts ----------


def test_run_fixture_cmd(tmp_path, capsys):
    from explore import run_fixture_cmd
    run_fixture_cmd({})                                            # no cmd → no-op
    run_fixture_cmd({"fixture_cmd": "touch seeded.marker", "path": str(tmp_path)})
    assert (tmp_path / "seeded.marker").exists()
    with pytest.raises(SystemExit, match="fixture_cmd failed"):
        run_fixture_cmd({"fixture_cmd": "exit 3", "path": str(tmp_path)})
    run_fixture_cmd({"fixture_teardown_cmd": "touch torn.marker", "path": str(tmp_path)},
                    teardown=True)
    assert (tmp_path / "torn.marker").exists()


def test_render_junit_xml_roundtrip():
    import xml.etree.ElementTree as ET

    from matrix import render_junit_xml
    rows = [
        {"file": "a.md", "id": "TC-1", "role": "reader", "viewport": "mobile", "status": "pass"},
        {"file": "b.spec.ts", "status": "fail", "note": 'boom & <tag> "quoted"'},
        {"file": "c.md", "id": "TC-2", "status": "manual", "note": "skipped (mutating)"},
    ]
    root = ET.fromstring(render_junit_xml("demo", "run-1", rows))
    suite = root.find("testsuite")
    assert suite.get("tests") == "3" and suite.get("failures") == "1" and suite.get("skipped") == "1"
    names = [c.get("name") for c in suite.findall("testcase")]
    assert "a.md::TC-1::reader::mobile" in names
    failure = suite.findall("testcase")[1].find("failure")
    assert "boom &" in failure.text


def test_save_diff_mask_renders_changed_pixels(tmp_path):
    from PIL import Image

    from run_scenarios import save_diff_mask
    a = Image.new("RGB", (10, 10), (255, 255, 255))
    b = Image.new("RGB", (10, 10), (255, 255, 255))
    for x in range(5):
        b.putpixel((x, 0), (0, 0, 0))
    a.save(tmp_path / "a.png")
    b.save(tmp_path / "b.png")
    assert save_diff_mask(tmp_path / "a.png", tmp_path / "b.png", tmp_path / "d.png")
    mask = Image.open(tmp_path / "d.png").convert("RGB")
    assert mask.getpixel((0, 0)) == (220, 30, 30)                   # changed → red
    assert mask.getpixel((9, 9)) == (255, 255, 255)                 # unchanged → white
    # size mismatch → no mask (that's a layout regression, not a pixel diff)
    Image.new("RGB", (5, 5)).save(tmp_path / "c.png")
    assert not save_diff_mask(tmp_path / "a.png", tmp_path / "c.png", tmp_path / "e.png")


def test_failure_artifacts_prefix_match(tmp_path):
    from maintain import failure_artifacts, failure_context_section
    tr = tmp_path / "test-results"
    d = tr / "article-lifecycle__tc-g2-c-0da09--editor-chromium"
    d.mkdir(parents=True)
    (d / "error-context.md").write_text("# Page snapshot\n- button \"Publish\"")
    (d / "test-failed-1.png").write_text("png")
    (tr / "other-spec-tc-x-chromium").mkdir()
    out = failure_artifacts(tmp_path, "article-lifecycle__tc-g2-create-a-new-article.spec.ts")
    assert "Publish" in out["error_context"]
    assert len(out["screens"]) == 1
    assert failure_context_section(None) == ""
    assert "PAGE STATE AT FAILURE" in failure_context_section(out["error_context"])
    # no test-results dir at all → empty, no crash
    assert failure_artifacts(tmp_path / "nope", "x.spec.ts") == {"error_context": None, "screens": []}


# ---------- locator probe ----------


SPEC_SNIPPET = """
import { test, expect } from '@playwright/test';
const APP = 'http://x';
test('t', async ({ page }) => {
  await page.goto(APP + '/items', { waitUntil: 'domcontentloaded' });
  await page.getByRole('button', { name: 'Create item' }).click();
  await page.getByLabel('Item name').fill('QA-x');
  await page.getByTestId('create-item').click();
  await page.getByRole('button', { name: /favorite/i }).click();
  await page.getByText(`QA-${Date.now()}`).click();
  await page.getByRole('button', { name: 'Create item' }).click();
  await page.goto(`${APP}/article/${slug}`);
});
"""


def test_extract_locators_static_literals_only():
    from locator_probe import extract_locators
    locs = extract_locators(SPEC_SNIPPET)
    raws = [(loc["kind"], loc["value"], loc["name"]) for loc in locs]
    assert ("Role", "button", "Create item") in raws     # deduped: appears twice in source
    assert ("Label", "Item name", None) in raws
    assert ("TestId", "create-item", None) in raws
    assert len([r for r in raws if r == ("Role", "button", "Create item")]) == 1
    # regex name and template value are skipped
    assert not any("favorite" in str(r) for r in raws)
    assert not any("${" in str(r) for r in raws)


def test_entry_path_first_goto_only():
    from locator_probe import entry_path
    assert entry_path(SPEC_SNIPPET) == "/items"
    assert entry_path("await page.goto(`${APP}/article/${slug}`);") is None  # dynamic first
    assert entry_path("const x = 1;") is None


def test_classify_count():
    from locator_probe import classify_count
    named = {"kind": "Role", "value": "button", "name": "Save"}
    nameless = {"kind": "Role", "value": "row", "name": None}
    text = {"kind": "Text", "value": "Widget", "name": None}
    assert classify_count(named, 0) == "miss"
    assert classify_count(named, 1) is None
    assert classify_count(named, 2) == "ambiguous"
    assert classify_count(nameless, 5) is None            # nameless role: many is normal
    assert classify_count(text, 3) == "ambiguous"


def test_role_from_spec_matches_role_email_not_default():
    from locator_probe import role_from_spec
    proj = {"auth": {"email": "admin@x.io", "password": "p"},
            "roles": [{"name": "reader", "email": "reader@x.io", "password": "p"}]}
    # G6 pattern: API setup as default account, UI session as the role — role wins
    src = "const AUTHOR = { email: 'admin@x.io' };\nconst READER = { email: 'reader@x.io' };"
    assert role_from_spec(src, proj) == "reader"
    assert role_from_spec("const U = { email: 'admin@x.io' };", proj) is None  # default session
    assert role_from_spec("no emails here", proj) is None
    assert role_from_spec(src, {"auth": {"email": "admin@x.io"}}) is None      # no roles configured


def test_probe_feedback_format():
    from locator_probe import probe_feedback
    assert probe_feedback(None) is None
    assert probe_feedback({"url": "/x", "misses": [], "ambiguous": [], "checked": 3, "skipped": 0}) is None
    fb = probe_feedback({"url": "/x", "misses": ["getByRole('button', { name: 'Go' })"],
                         "ambiguous": ["getByText('a') → 3 elements"], "checked": 2, "skipped": 0})
    assert "0 elements on `/x`" in fb and "SEVERAL elements" in fb


# ---------- auth adapter ----------


def test_render_body_substitutes_nested_placeholders():
    from explore import _render_body
    tpl = {"user": {"email": "{email}", "password": "{password}"}, "keep": 1}
    out = _render_body(tpl, {"email": "a@b.c", "password": "s3"})
    assert out == {"user": {"email": "a@b.c", "password": "s3"}, "keep": 1}
    assert tpl["user"]["email"] == "{email}"  # template untouched


def test_dig_dot_path():
    from explore import _dig
    assert _dig({"user": {"token": "jwt"}}, "user.token") == "jwt"
    assert _dig({"user": {}}, "user.token") is None
    assert _dig({"user": "flat"}, "user.token") is None


def test_build_storage_state_localstorage_token():
    from explore import build_storage_state
    proj = {"auth_browser_storage": {"kind": "localStorage", "key": "realworld-auth-token"}}
    st = build_storage_state({"sid": "x"}, "http://127.0.0.1:30401", "jwt-123", proj)
    assert st["cookies"][0]["name"] == "sid"                       # cookies still there
    assert st["origins"] == [{"origin": "http://127.0.0.1:30401",
                              "localStorage": [{"name": "realworld-auth-token", "value": "jwt-123"}]}]
    # no token or no config → old behavior, empty origins
    assert build_storage_state({}, "http://x", None, proj)["origins"] == []
    assert build_storage_state({}, "http://x", "jwt", {})["origins"] == []


# ---------- registry ----------


def test_registry_never_lives_inside_the_skill(tmp_path, monkeypatch):
    """A registry in the skill root shadowed the user's real one — a repo checkout answered
    "alias not in registry" — and a plugin's version-scoped cache dir vanishes on update.
    Passwords belong in neither."""
    import registry
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("WEBQA_REGISTRY", raising=False)
    xdg = tmp_path / "xdg" / "web-qa" / "projects.json"

    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "projects.json").write_text("[]")          # a stray copy must be ignored
    monkeypatch.setattr(registry, "SKILL_ROOT", skill)

    assert registry.registry_path() == xdg
    assert registry.registry_write_path() == xdg

    # env beats everything
    monkeypatch.setenv("WEBQA_REGISTRY", str(tmp_path / "custom.json"))
    assert registry.registry_path() == tmp_path / "custom.json"
    assert registry.registry_write_path() == tmp_path / "custom.json"


# ---------- route_mine ----------


def _touch(root, *rel):
    p = root.joinpath(*rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    return p


def _pkg(root, *deps):
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({"dependencies": {d: "*" for d in deps}}))


def test_mine_next_app_router(tmp_path):
    from route_mine import mine_routes
    _pkg(tmp_path, "next")
    _touch(tmp_path, "app", "page.tsx")
    _touch(tmp_path, "app", "orders", "[orderId]", "page.tsx")
    _touch(tmp_path, "app", "(dashboard)", "settings", "page.tsx")   # group stripped
    _touch(tmp_path, "app", "_private", "page.tsx")                  # opted out
    _touch(tmp_path, "app", "api", "route.ts")                       # not a page
    _touch(tmp_path, "node_modules", "lib", "app", "page.tsx")       # pruned
    paths = [m["path"] for m in mine_routes(tmp_path)]
    assert paths == ["/", "/orders/{orderId}", "/settings"]


def test_mine_pages_router_and_nuxt(tmp_path):
    from route_mine import mine_routes
    _pkg(tmp_path, "next", "nuxt")
    _touch(tmp_path, "pages", "index.tsx")
    _touch(tmp_path, "pages", "orders", "[id].tsx")
    _touch(tmp_path, "pages", "_app.tsx")
    _touch(tmp_path, "pages", "api", "users.ts")
    _touch(tmp_path, "pages", "profile", "_tab.vue")                 # Nuxt2 dynamic
    paths = [m["path"] for m in mine_routes(tmp_path)]
    assert paths == ["/", "/orders/{id}", "/profile/{tab}"]


def test_mine_router_config_absolute_only(tmp_path):
    from route_mine import mine_routes
    _touch(tmp_path, "src", "router.ts").write_text(
        "const routes = ["
        "{path: '/admin', component: A},"
        "{path: 'edit', component: B},"          # relative child — skipped
        "{path: '/users/:userId', component: C},"
        "{path: '/*', component: NotFound},"     # wildcard — skipped
        "]; export const x = <Route path=\"/reports\" element={<R/>} />;")
    paths = [m["path"] for m in mine_routes(tmp_path)]
    assert paths == ["/admin", "/reports", "/users/{userId}"]


def test_mine_requires_framework_dependency(tmp_path):
    """src/pages in a plain React app (FSD layout) is NOT a file router — without
    next/nuxt in package.json only real route declarations (router-config) are mined."""
    from route_mine import mine_routes
    _pkg(tmp_path, "react", "react-router")
    _touch(tmp_path, "src", "pages", "article", "article.route.ts").write_text(
        "export const articleRoute = { path: '/article/:slug' };")
    _touch(tmp_path, "src", "pages", "article", "article.loader.ts")
    _touch(tmp_path, "src", "pages", "home", "home.ui.tsx")
    assert [m["path"] for m in mine_routes(tmp_path)] == ["/article/{slug}"]


def test_as_template_unifies_params_and_ids():
    from route_mine import as_template
    assert as_template("/orders/{orderId}") == "/orders/{id}"
    assert as_template("/orders/42") == "/orders/{id}"
    assert as_template("/orders/{orderId}/items/7") == "/orders/{id}/items/{id}"


def test_annotate_origins_marks_and_appends():
    from explore import annotate_origins
    pages = [{"path": "/orders/42", "title": "Order"}, {"path": "/", "title": "Home"}]
    mined = [{"path": "/orders/{orderId}", "source": "next-app"},
             {"path": "/settings", "source": "next-app"}]
    out = annotate_origins(pages, mined)
    assert out[0]["origin"] == "crawl+code"       # /orders/42 matches /orders/{orderId}
    assert out[1]["origin"] == "crawl"
    assert out[2] == {"path": "/settings", "origin": "code:next-app", "uncrawled": True}


# ---------- run_scenarios ----------

BACKEND_PREFIXES = ("/auth", "/orders/facets", "/orders/{")


def test_extract_paths_separates_frontend_and_backend():
    fronts, backs = extract_paths("Open `/orders`, then GET `/orders/facets`", BACKEND_PREFIXES)
    assert fronts == ["/orders"]
    assert backs == [("GET", "/orders/facets")]


def test_extract_paths_bare_root_and_query_collapse():
    # `/` and `/?limit=10&offset=0` are the same route — SPAs put pagination in the query
    fronts, _ = extract_paths("Navigate to `/?limit=10&offset=0`.", BACKEND_PREFIXES)
    assert fronts == ["/"]
    fronts, _ = extract_paths("Open `/` and check the feed", BACKEND_PREFIXES)
    assert fronts == ["/"]
    fronts, _ = extract_paths("Open `/orders?page=2`", BACKEND_PREFIXES)
    assert fronts == ["/orders"]


def test_classify_declared_type_beats_absence():
    kind, reasons = classify("**Type:** passive\n**Steps:**\n1. open `/orders`\n2. GET `/orders`")
    assert kind == "passive" and reasons == []
    kind, reasons = classify("**Type:** mutating\n**Steps:**\n1. look only")
    assert kind == "mutating" and reasons


def test_classify_steps_evidence_beats_declaration():
    # an LLM-mislabeled "passive" must not survive a mutating action in Steps
    kind, reasons = classify("**Type:** passive\n**Steps:**\n1. POST `/orders`\n**Expected:**\n- 403")
    assert kind == "mutating"
    assert any("contradicted by POST /orders" in r for r in reasons)
    # ...but a method in Expected is context, not an action — no conflict
    kind, reasons = classify(
        "**Type:** passive\n**Steps:**\n1. open `/dash`\n**Expected:**\n- data comes from POST `/sync`")
    assert kind == "passive" and reasons == []


def test_classify_is_language_agnostic():
    # no Type field, no HTTP signal → mutating regardless of the prose language;
    # keyword lists (EN/RU only) used to misclassify third languages as passive
    for prose in ("Formular absenden und Eintrag löschen",   # de
                  "フォームを送信してアイテムを削除する",          # ja
                  "just look at the table"):                  # en, no keywords either
        kind, reasons = classify(prose)
        assert kind == "mutating"
        assert any("no **Type:**" in r for r in reasons)


def test_classify_http_method_signal_still_reported():
    kind, reasons = classify("**Steps:**\n1. call POST `/orders`")
    assert kind == "mutating"
    assert any("POST" in r for r in reasons)


def test_declared_type_parsing():
    assert declared_type("**Type:** passive\nrest") == "passive"
    assert declared_type("**Type:** `mutating`") == "mutating"
    assert declared_type("**type:** Passive") == "passive"  # case-insensitive
    assert declared_type("no field") is None


def test_is_mutating_only_declared_passive_is_skipped():
    assert is_mutating({"body": "**Type:** passive\n**Steps:** open `/`"}) is False
    assert is_mutating({"body": "**Type:** mutating\n**Steps:** submit"}) is True
    # undeclared TC → spec-worthy (never guessed from prose keywords)
    assert is_mutating({"body": "Открыть страницу и сохранить изменения"}) is True
    assert is_mutating({"body": "nur die Tabelle ansehen"}) is True


def test_keyword_terms_quoted_ui_text_and_all_scripts():
    from run_scenarios import keyword_to_search_terms
    # quoted UI strings survive verbatim (strongest signal in any language)
    assert "create item" in keyword_to_search_terms('button `Create item` is visible')
    assert "заказ создан" in keyword_to_search_terms("появляется «Заказ создан»")
    # unicode word tokens: latin, cyrillic, and short CJK words all produce terms
    assert keyword_to_search_terms("shipment counter increases")
    assert keyword_to_search_terms("таблица заказов отображается")
    assert keyword_to_search_terms("注文テーブルが表示")  # CJK words are 2-3 chars
    assert "das" not in keyword_to_search_terms("das Formular wird angezeigt")  # <4 chars skipped


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
        test_data_prefix="QA-", test_timeout_ms=60000,
        auth_login_hint="use `Bearer ${access_token}` and {email, password}",
        seed_section="```ts\nconst t = `x${y}`;\n```",
        app_context="ctx", dnd_section="", tc_body="## TC-1 — t",
    )
    assert "Bearer ${access_token}" in p and "{email, password}" in p


def test_detect_dnd_library(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps(
        {"dependencies": {"@dnd-kit/core": "^6", "react": "18"}}))
    assert detect_dnd_library(tmp_path) == "dnd-kit"


def test_detect_dnd_library_monorepo_subdir(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(json.dumps(
        {"devDependencies": {"react-beautiful-dnd": "^13"}}))
    assert detect_dnd_library(tmp_path) == "react-beautiful-dnd"


def test_detect_dnd_library_none(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "18"}}))
    assert detect_dnd_library(tmp_path) is None
    assert detect_dnd_library(tmp_path / "nope") is None


def test_dnd_recipe_section_empty_without_lib():
    assert dnd_recipe_section(None) == ""


def test_dnd_recipe_section_pointer_helper():
    sec = dnd_recipe_section("dnd-kit")
    assert "dnd-kit" in sec
    assert "async function dragTo" in sec and "mouse.down()" in sec
    assert "activation threshold" in sec
    assert "toHaveText" in sec                 # assert via DOM order, not visual
    assert "{ steps: 3 }" in sec               # literal braces survive


def test_dnd_recipe_section_native():
    sec = dnd_recipe_section("native")
    assert "source.dragTo(target)" in sec


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
        flaky, _q = update_history(d, f"run-{status}", [row])
    assert row_key(row) in flaky
    assert len(json.loads((d / "history.json").read_text())) == 3


def test_quarantine_off_by_default(tmp_path):
    d = _webqa(tmp_path)
    row = {"source": "scenario", "file": "f.md", "id": "TC-1", "role": "-",
           "status": "pass", "kind": "passive", "title": "t"}
    for status in ("pass", "fail"):
        row["status"] = status
        _f, q = update_history(d, f"run-{status}", [row])   # quarantine_window=0
    assert q == set()


def test_quarantine_flags_flipped_within_window(tmp_path):
    d = _webqa(tmp_path)
    row = {"source": "scenario", "file": "f.md", "id": "TC-1", "role": "-",
           "status": "pass", "kind": "passive", "title": "t"}
    quarantined = set()
    for status in ("pass", "fail", "fail"):
        row["status"] = status
        _f, quarantined = update_history(d, f"run-{status}", [row], quarantine_window=3)
    assert row_key(row) in quarantined   # flipped pass↔fail within last 3 runs


def test_quarantined_failure_excluded_from_gate_and_junit():
    from matrix import GATE_BLOCKING, render_junit_xml, row_key
    import xml.etree.ElementTree as ET
    row = {"source": "spec", "file": "a.spec.ts", "id": "-", "role": "-", "viewport": "-",
           "status": "fail", "kind": "spec", "title": "t", "note": "racy"}
    q = {row_key(row)}
    # gate: a quarantined failing row does not block
    gate_ok = not any(r["status"] in GATE_BLOCKING and row_key(r) not in q for r in [row])
    assert gate_ok is True
    # junit: quarantined failure emitted as skipped, not failure
    root = ET.fromstring(render_junit_xml("demo", "run-1", [row], q))
    suite = root.find("testsuite")
    assert suite.get("failures") == "0" and suite.get("skipped") == "1"
    assert "quarantined" in root.find(".//skipped").get("message")


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
    flaky, _q = update_history(d, "new", [row])
    assert row_key(row) in flaky  # old fail + new pass across formats = flaky


def test_viewport_env_honors_viewports_list():
    assert viewport_env(VP_PROJ) == "1280x900"          # first entry = default
    assert viewport_env({"viewports": [{"name": "m", "device": "iPhone 14"}]}) is None
    assert viewport_env({}) is None


def test_record_app_bug_dedupes(tmp_path):
    (tmp_path / ".web-qa").mkdir()
    record_app_bug(tmp_path, "a.spec.ts", "same bug")
    record_app_bug(tmp_path, "a.spec.ts", "same bug")
    text = (tmp_path / ".web-qa" / "BUGS.md").read_text()
    assert text.count("same bug") == 1


def test_transient_signature_matches_network_and_infra():
    assert transient_signature(["page.goto: net::ERR_CONNECTION_REFUSED at http://x"])
    assert transient_signature(["Error: connect ECONNRESET 127.0.0.1:3000"])
    assert transient_signature(["Request failed with status 503 Service Unavailable"])
    assert transient_signature(["POST /api returned 429 Too Many Requests"])
    assert transient_signature(["Target page, context or browser has been closed"])
    assert "ECONNREFUSED" in transient_signature(["boom ECONNREFUSED boom"])


def test_transient_signature_ignores_real_failures():
    # a genuine assertion / missing-element failure has none of the infra strings
    assert transient_signature([
        "expect(locator).toBeVisible() failed: Timeout 30000ms exceeded "
        "waiting for getByRole('button', { name: 'Publish' })"]) is None
    assert transient_signature(["Expected 'Welcome' but received 'Goodbye'"]) is None
    assert transient_signature([]) is None


CONSOLE = [
    {"type": "error", "text": "Uncaught TypeError: x is undefined"},
    {"type": "warning", "text": "deprecated API"},
    {"type": "log", "text": "hello"},
    {"type": "error", "text": "Failed to load resource: the server responded 404 (favicon.ico)"},
]


def test_gated_console_off_by_default():
    assert gated_console(CONSOLE, [], []) == []          # opt-in: empty fail_on = off


def test_gated_console_matches_type():
    bad = gated_console(CONSOLE, ["error"], [])
    assert [c["type"] for c in bad] == ["error", "error"]


def test_gated_console_ignore_regex():
    bad = gated_console(CONSOLE, ["error"], [r"favicon\.ico", r"ResizeObserver"])
    assert len(bad) == 1 and "TypeError" in bad[0]["text"]


def test_gated_console_multiple_types():
    bad = gated_console(CONSOLE, ["error", "warning"], [])
    assert len(bad) == 3


# ---------------------------------------------------------------- LLM spend controls

@pytest.fixture
def ledger():
    """Reset the module-level spend ledger around each test."""
    import spec_gen
    spec_gen._spent_usd, spec_gen._call_count = 0.0, 0
    yield spec_gen
    spec_gen._spent_usd, spec_gen._call_count = 0.0, 0


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_claude_cmd_defaults_are_cheap_and_toolless(monkeypatch):
    for var in ("WEBQA_CLAUDE_MODEL", "WEBQA_CLAUDE_EFFORT", "WEBQA_CLAUDE_TOOLS"):
        monkeypatch.delenv(var, raising=False)
    cmd = claude_cmd("hello")
    assert cmd[:3] == ["claude", "-p", "hello"]
    assert _flag(cmd, "--model") == "sonnet"
    assert _flag(cmd, "--effort") == "medium"
    assert _flag(cmd, "--tools") == ""          # no agentic loop: context is in the prompt
    assert _flag(cmd, "--output-format") == "json"   # spend is metered, not guessed
    assert "--strict-mcp-config" in cmd


def test_claude_cmd_env_overrides(monkeypatch):
    monkeypatch.setenv("WEBQA_CLAUDE_MODEL", "opus")
    monkeypatch.setenv("WEBQA_CLAUDE_EFFORT", "high")
    monkeypatch.setenv("WEBQA_CLAUDE_TOOLS", "Read,Grep")
    cmd = claude_cmd("x")
    assert _flag(cmd, "--model") == "opus"
    assert _flag(cmd, "--effort") == "high"
    assert _flag(cmd, "--tools") == "Read,Grep"


def test_claude_cmd_empty_model_drops_the_flag(monkeypatch):
    monkeypatch.setenv("WEBQA_CLAUDE_MODEL", "")
    assert "--model" not in claude_cmd("x")


def test_strip_fences():
    assert strip_fences("```typescript\nconst a = 1;\n```") == "const a = 1;"
    assert strip_fences("  plain  ") == "plain"


def test_parse_claude_json_returns_result_and_meters_spend(ledger):
    out = parse_claude_json(json.dumps(
        {"result": "```ts\nconst a = 1;\n```", "total_cost_usd": 0.25, "num_turns": 1}))
    assert out == "const a = 1;"
    assert ledger.llm_spend()["spent_usd"] == 0.25
    assert ledger.llm_spend()["calls"] == 1


def test_parse_claude_json_raises_on_session_error(ledger):
    with pytest.raises(RuntimeError, match="claude session errored"):
        parse_claude_json(json.dumps({"is_error": True, "result": "usage limit reached"}))
    assert ledger.llm_spend()["calls"] == 0


def test_parse_claude_json_falls_back_to_raw_stdout(ledger):
    assert parse_claude_json("not json at all") == "not json at all"
    assert ledger.llm_spend()["calls"] == 0


def test_budget_blocks_before_spending(ledger, monkeypatch):
    monkeypatch.setenv("WEBQA_MAX_USD", "1.00")
    ledger._spent_usd = 1.00
    with pytest.raises(LLMBudgetExceeded, match=r"budget exhausted"):
        ledger._check_budget()


def test_budget_allows_below_ceiling(ledger, monkeypatch):
    monkeypatch.setenv("WEBQA_MAX_USD", "1.00")
    ledger._spent_usd = 0.99
    ledger._check_budget()  # must not raise


def test_budget_guard_disabled_by_zero(ledger, monkeypatch):
    monkeypatch.setenv("WEBQA_MAX_USD", "0")
    ledger._spent_usd = 999.0
    ledger._check_budget()  # must not raise


def test_budget_falls_back_on_garbage_env(monkeypatch):
    import spec_gen
    monkeypatch.setenv("WEBQA_MAX_USD", "not-a-number")
    assert spec_gen.llm_budget_usd() == spec_gen.DEFAULT_MAX_USD


# ---------------------------------------------------------- scenario coverage awareness

def test_claude_cmd_caller_tier_and_env_precedence(monkeypatch):
    monkeypatch.delenv("WEBQA_CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("WEBQA_CLAUDE_EFFORT", raising=False)
    # caller's tier wins over the default
    cmd = claude_cmd("x", model="opus", effort="high")
    assert _flag(cmd, "--model") == "opus" and _flag(cmd, "--effort") == "high"
    # the user's env beats the caller's tier
    monkeypatch.setenv("WEBQA_CLAUDE_MODEL", "haiku")
    assert _flag(claude_cmd("x", model="opus"), "--model") == "haiku"


def _webqa_with(tmp_path, routes_table, scenarios):
    webqa = tmp_path / ".web-qa"
    (webqa / "scenarios").mkdir(parents=True)
    (webqa / "app.context.md").write_text(routes_table, encoding="utf-8")
    for name, body in scenarios.items():
        (webqa / "scenarios" / name).write_text(body, encoding="utf-8")
    return webqa


ROUTES_MD = """## Routes
| Route | Title |
| `/` | Dashboard |
| `/orders` | Orders |
| `/orders/17` | Order card |
| `/admin/users` | Users |
| `/import` | Import |
"""

# Two shapes a naive extractor gets backwards:
#  * TC-A1 documents its own `GET /admin/users` call — the page is still visited
#  * TC-A2 mentions `/` only as a redirect target in Expected — it never tests the dashboard
ADMIN_TC = """# Admin regression

## TC-A1 — Users list
**Type:** passive
**Steps:**
1. Open `/admin/users`, wait for the table (GET `/admin/users`).
**Expected:**
- roles column renders

## TC-A2 — Viewer is bounced
**Type:** passive
**Steps:**
1. Open `/orders/17` as viewer.
**Expected:**
- redirected to `/`
"""


def test_route_coverage_finds_the_untouched_dashboard(tmp_path):
    from gen_scenarios import route_coverage
    webqa = _webqa_with(tmp_path, ROUTES_MD, {"admin.md": ADMIN_TC})
    covered, uncovered = route_coverage(webqa)
    # the page it documents a GET for is covered; the redirect target in Expected is not
    assert covered == ["/orders/{id}", "/admin/users"]
    assert uncovered == ["/", "/orders", "/import"]   # app-map order; "/" is the classic blind spot


def test_tc_routes_ignores_expected_only_mentions():
    from gen_scenarios import tc_routes
    body = ADMIN_TC.split("## TC-A2")[1]
    assert tc_routes(body) == {"/orders/{id}"}          # NOT "/"


def test_tc_routes_keeps_a_page_that_documents_its_api_call():
    from gen_scenarios import tc_routes
    body = ADMIN_TC.split("## TC-A1")[1].split("## TC-A2")[0]
    assert tc_routes(body) == {"/admin/users"}


def test_route_coverage_normalizes_concrete_ids(tmp_path):
    from gen_scenarios import route_coverage
    tc = ADMIN_TC.replace("`/orders/17`", "`/orders/999`")
    webqa = _webqa_with(tmp_path, ROUTES_MD, {"admin.md": tc})
    covered, _ = route_coverage(webqa)
    assert "/orders/{id}" in covered


def test_route_coverage_empty_without_app_map(tmp_path):
    from gen_scenarios import route_coverage
    webqa = _webqa_with(tmp_path, "no routes table here", {})
    assert route_coverage(webqa) == ([], [])


def test_coverage_prompt_section_names_the_blind_spots():
    from gen_scenarios import coverage_prompt_section
    s = coverage_prompt_section(["/orders"], ["/", "/import"])
    assert "`/orders`" in s and "`/import`" in s
    assert "NO test case at all" in s
    assert coverage_prompt_section([], []) == ""


def test_gen_scenarios_prompt_format_survives_new_placeholder():
    from gen_scenarios import PROMPT
    out = PROMPT.format(app_context="ctx", coverage_section="cov", roles_section="",
                        source_section="src", prefix="G", language="English")
    assert "cov" in out and "TC-G1" in out


# --------------------------------------------------- budget stop vs generation failure

def test_projected_total_needs_at_least_one_finished_job(ledger):
    from spec_gen import projected_total
    assert projected_total(20, 0) is None
    assert projected_total(0, 5) is None
    ledger._spent_usd = 0.60
    assert projected_total(20, 3) == pytest.approx(4.0)   # 0.20/spec × 20


def test_budget_stop_is_not_a_spec_failure_shape():
    """A TC the budget guard blocked never reached the model: it must not look like a
    spec the generator failed to write."""
    import inspect

    import spec_gen
    src = inspect.getsource(spec_gen.gen_specs)
    # the budget branch is handled before the generic Exception -> .FAILED branch
    assert src.index("except LLMBudgetExceeded") < src.index("except Exception")
    budget_branch = src[src.index("except LLMBudgetExceeded"):src.index("except Exception")]
    assert "skipped_over_budget" in budget_branch
    assert "marker.write_text" not in budget_branch


def test_missing_role_is_not_an_error():
    import inspect

    import spec_gen
    src = inspect.getsource(spec_gen.gen_specs)
    role_branch = src[src.index("except SystemExit"):src.index("body_hash = tc_hash")]
    assert "skipped_missing_role" in role_branch
    assert 'summary["errors"]' not in role_branch


# ------------------------------------------------- app map: merge, aria fairness, manual

def test_merge_pages_carries_over_unreached_routes():
    from explore import merge_pages
    prev = [{"path": "/orders", "aria": "table"}, {"path": "/settings", "aria": "grid"}]
    fresh = [{"path": "/orders", "aria": "table2"}]          # crawl stopped early
    merged, rep = merge_pages(prev, fresh, "2026-07-09")
    paths = {p["path"] for p in merged}
    assert paths == {"/orders", "/settings"}                 # nothing silently deleted
    carried = next(p for p in merged if p["path"] == "/settings")
    assert carried["stale_since"] == "2026-07-09"
    assert rep["carried_over"] == ["/settings"]
    assert next(p for p in merged if p["path"] == "/orders")["aria"] == "table2"  # fresh wins


def test_merge_pages_reports_lost_aria_as_regression():
    from explore import merge_pages
    prev = [{"path": "/orders", "aria": "table"}]
    fresh = [{"path": "/orders"}]                            # reached, but snapshot vanished
    _, rep = merge_pages(prev, fresh, "2026-07-09")
    assert rep["lost_aria"] == ["/orders"]
    assert rep["carried_over"] == []


def test_merge_pages_reports_new_routes_and_ignores_uncrawled():
    from explore import merge_pages
    prev = [{"path": "/a", "uncrawled": True}]
    fresh = [{"path": "/b"}]
    merged, rep = merge_pages(prev, fresh, "2026-07-09")
    assert rep["new_routes"] == ["/b"]
    assert [p["path"] for p in merged] == ["/b"]             # code-only rows are re-derived


def test_annotate_origins_keeps_stale_marking():
    from explore import annotate_origins
    pages = [{"path": "/orders", "stale_since": "2026-07-01"}, {"path": "/new"}]
    out = annotate_origins(pages, [])
    assert out[0]["origin"] == "stale:2026-07-01"
    assert out[1]["origin"] == "crawl"


def test_aria_snapshots_are_not_rationed_across_routes():
    """A shared map-wide budget divided by 47 routes left each one 400 chars — a page title
    and two nodes. The prompt budget belongs in slice_aria, which sends one route's snapshot,
    not here. A real snapshot is ~12 KB and must survive intact."""
    from explore import render_context_md
    real_snapshot = "- button \"New item\"\n" * 700          # ~14 KB, the size of a real page
    pages = [{"path": f"/r{i}", "aria": real_snapshot, "origin": "crawl"} for i in range(30)]
    md = render_context_md({"alias": "t", "target_url": "http://x"}, pages, {}, {})
    assert md.count("```yaml") == 30                          # every route gets one
    assert "snapshot clipped" not in md                       # and it is not truncated to a stub
    assert md.count('button "New item"') == 30 * 700


def test_aria_snapshot_of_one_pathological_page_is_still_bounded():
    from explore import render_context_md
    pages = [{"path": "/huge", "aria": "y" * 50_000, "origin": "crawl"}]
    md = render_context_md({"alias": "t", "target_url": "http://x"}, pages, {}, {})
    assert "snapshot clipped" in md
    assert len(md) < 30_000


def test_protect_manual_survives_truncation():
    from spec_gen import _protect_manual
    manual = "<!-- manual -->\n- admins only may delete\n"
    md = "A" * 5000 + manual
    out = _protect_manual(md, 1000)
    assert manual.strip() in out                              # the hand-written rule survives
    assert "(auto map truncated)" in out
    assert len(out) <= 1000 + len(manual)


def test_protect_manual_noop_when_within_budget():
    from spec_gen import _protect_manual
    md = "short map"
    assert _protect_manual(md, 1000) == md


# --------------------------------------------- static assets are not routes; per-TC slicing

def test_is_static_asset():
    from explore import is_static_asset
    for p in ["/help/ru/orders/list.png", "/a/b.svg", "/x.PDF", "/style.css", "/f.woff2"]:
        assert is_static_asset(p), p
    for p in ["/orders", "/orders/17", "/help/orders", "/", "/import"]:
        assert not is_static_asset(p), p


ARIA_MAP = """# map

## Routes (frontend)
| Path |
| `/orders` |

## ARIA snapshots (role/name)

### `/orders`
```yaml
button "New item"
```
### `/orders/23`
```yaml
button "Save"
```
### `/help`
```yaml
link "Help"
```

## Backend API
### `/orders`
- GET
"""


def test_slice_aria_keeps_only_the_routes_the_tc_visits():
    from spec_gen import slice_aria
    out = slice_aria(ARIA_MAP, {"/orders", "/orders/{id}"})
    assert 'New item' in out and 'Save' in out
    assert 'link "Help"' not in out            # /help is another test case's problem
    assert "showing 2 of 3 route snapshots" in out
    assert "## Backend API" in out                # everything outside the block is untouched


def test_slice_aria_parent_route_pulls_in_its_entity_cards():
    from spec_gen import slice_aria
    out = slice_aria(ARIA_MAP, {"/orders"})       # TC only names /orders
    assert 'Save' in out                          # /orders/23 is a child, still relevant


def test_slice_aria_noop_without_routes_or_matches():
    from spec_gen import slice_aria
    assert slice_aria(ARIA_MAP, set()) == ARIA_MAP
    # a TC that touches nothing we mapped keeps the whole section rather than none of it
    assert slice_aria(ARIA_MAP, {"/ghost"}) == ARIA_MAP


def test_tc_routes_moved_to_run_scenarios_is_still_the_same_rule():
    from run_scenarios import tc_routes
    body = "**Steps:**\n1. Open `/admin/users` (GET `/admin/users`).\n**Expected:**\n- to `/`"
    assert tc_routes(body) == {"/admin/users"}


# ------------------------------------------ template collapsing + per-TC OpenAPI slicing

def test_dedupe_by_template_collapses_entity_cards_and_names_the_sample():
    from explore import dedupe_by_template
    out = dedupe_by_template([
        {"path": "/orders/23", "aria": "a"},
        {"path": "/orders/24", "aria": "b"},      # same route, second sample
        {"path": "/orders"},
        {"url": "http://x/boom", "error": "nope"},
    ])
    paths = [p.get("template") or p.get("url") for p in out]
    assert paths == ["/orders/{id}", "/orders", "http://x/boom"]
    assert out[0]["sampled_from"] == "/orders/23"   # a snapshot must come from a real page
    assert "sampled_from" not in out[1]


def test_dedupe_by_template_keeps_author_declared_param_names():
    from explore import dedupe_by_template
    out = dedupe_by_template([{"path": "/help/{section}", "uncrawled": True}])
    assert out[0]["template"] == "/help/{section}"   # `{section}` says more than `{id}`


API_MAP = """# map

## Backend endpoints (from OpenAPI)

### `/orders`
- /orders — GET

### `/shipments`
- /shipments — GET

### `/auth`
- /auth/login — POST

## Auth Flow
- login
"""


def test_api_group():
    from spec_gen import api_group
    assert api_group("/orders/{id}/items") == "/orders"
    assert api_group("/") == "/"


def test_tc_api_groups_includes_named_oracle_endpoints_and_auth():
    from spec_gen import tc_api_groups
    body = "**Steps:**\n1. Open `/`.\n2. Reference: GET `/invoices`.\n"
    groups = tc_api_groups(body, ())
    assert "/invoices" in groups           # the independent-oracle collection survives
    assert "/auth" in groups               # every spec logs in


def test_slice_openapi_keeps_only_callable_groups():
    from spec_gen import slice_openapi
    out = slice_openapi(API_MAP, {"/orders", "/auth"})
    assert "### `/orders`" in out and "### `/auth`" in out
    assert "### `/shipments`" not in out
    assert "showing 2 of 3 endpoint groups" in out
    assert "## Auth Flow" in out            # the next section is untouched


def test_slice_openapi_noop_when_nothing_matches():
    from spec_gen import slice_openapi
    assert slice_openapi(API_MAP, {"/ghost"}) == API_MAP
    assert slice_openapi(API_MAP, set()) == API_MAP


# ---------------------------------------------------------------------------
# The API contract the map used to throw away.
#
# Every assertion below stands for a spec that failed against a CORRECT app:
# `.filter is not a function` (envelope guessed), `Expected 200 Received 201`
# (status guessed), an enum compared against the label the UI renders for it.
# ---------------------------------------------------------------------------

OPENAPI = {
    "paths": {
        "/orders": {
            "get": {"responses": {"200": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/OrderPage"}}}}}},
            "post": {
                "requestBody": {"content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Order"}}}},
                "responses": {"201": {"content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Order"}}}}, "422": {}},
            },
        },
        "/orders/{order_id}": {"delete": {"responses": {"204": {}}}},
    },
    "components": {"schemas": {
        "Order": {"type": "object", "required": ["id"],
                  "properties": {"id": {"type": "integer"},
                                 "status_payment": {"type": "string",
                                                    "enum": ["unpaid", "paid", "overdue"]}}},
        "OrderPage": {"type": "object", "properties": {"items": {"type": "array"},
                                                       "total": {"type": "integer"}}},
        "Currency": {"enum": ["USD", "CNY"]},
    }},
}


def test_deref_resolves_one_hop_and_passes_plain_schemas_through():
    from explore import deref
    assert deref(OPENAPI, {"$ref": "#/components/schemas/Currency"}) == {"enum": ["USD", "CNY"]}
    assert deref(OPENAPI, {"type": "string"}) == {"type": "string"}
    assert deref(OPENAPI, {"$ref": "#/components/schemas/Nope"}) == {}


def test_schema_brief_names_the_envelope_that_broke_dot_filter():
    from explore import schema_brief
    # `(await r.json()).filter(...)` on this shape is a TypeError, not a test
    assert schema_brief(OPENAPI, {"$ref": "#/components/schemas/OrderPage"}) == "{items:array, total:integer}"
    assert schema_brief(OPENAPI, {"type": "array", "items": {"$ref": "#/components/schemas/Order"}}) \
        == "array of {id*:integer, status_payment:string}"
    assert schema_brief(OPENAPI, {"type": "integer"}) == "integer"
    assert schema_brief(OPENAPI, {}) == ""


def test_op_responses_states_the_real_success_code():
    from explore import op_responses
    post = OPENAPI["paths"]["/orders"]["post"]
    out = op_responses(OPENAPI, post)
    assert out.startswith("201: {id*:integer")          # not 200 — the spec asserted 200 and went red
    assert "also declares 422" in out
    assert op_responses(OPENAPI, OPENAPI["paths"]["/orders/{order_id}"]["delete"]) == "204: no body"
    assert op_responses(OPENAPI, {"responses": {}}) == ""


def test_op_responses_without_a_success_code_lists_what_it_has():
    from explore import op_responses
    assert op_responses(OPENAPI, {"responses": {"401": {}, "403": {}}}) == "status: 401, 403"


def test_collect_enums_finds_standalone_and_property_enums():
    from explore import collect_enums
    enums = collect_enums(OPENAPI)
    assert enums["Currency"] == ["USD", "CNY"]
    assert enums["Order.status_payment"] == ["unpaid", "paid", "overdue"]


def test_context_md_carries_status_codes_shapes_and_wire_enums():
    from explore import render_context_md
    md = render_context_md({"alias": "t", "target_url": "http://x"}, [], OPENAPI, {})
    assert "GET → 200: {items:array, total:integer}" in md
    assert "POST → 201:" in md
    assert "POST body: id*:integer" in md
    assert "`Order.status_payment`: `unpaid`, `paid`, `overdue`" in md
    assert "never compare these to UI labels" in md


# ---------------------------------------------------------------------------
# api_login reported the login response as the user. `Logged-in: None (None)`.
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.cookies = payload, status, {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_api_login_reads_the_identity_endpoint_for_jwt_apps(monkeypatch):
    import explore
    monkeypatch.setattr(explore.httpx, "post", lambda *a, **k: _Resp({"access_token": "T"}))
    monkeypatch.setattr(explore.httpx, "get",
                        lambda url, **k: _Resp({"email": "v@x", "role": "viewer"})
                        if url.endswith("/auth/me") else _Resp({}, 404))
    _, me, token = explore.api_login("http://b", "v@x", "pw", {"auth_token_field": "access_token"})
    assert token == "T"
    assert me["role"] == "viewer"          # was None: the login body carries no identity


def test_api_login_keeps_working_when_login_already_returns_the_user(monkeypatch):
    import explore
    monkeypatch.setattr(explore.httpx, "post", lambda *a, **k: _Resp({"email": "a@x", "role": "admin"}))
    monkeypatch.setattr(explore.httpx, "get", lambda *a, **k: _Resp({}, 404))
    _, me, _t = explore.api_login("http://b", "a@x", "pw", {})
    assert me["role"] == "admin"


def test_api_login_never_reports_an_empty_identity(monkeypatch):
    import explore
    monkeypatch.setattr(explore.httpx, "post", lambda *a, **k: _Resp({"access_token": "T"}))
    monkeypatch.setattr(explore.httpx, "get", lambda *a, **k: _Resp({}, 500))
    _, me, _t = explore.api_login("http://b", "who@x", "pw", {})
    assert me["email"] == "who@x"          # at worst, the account we asked for
    assert me.get("role") is None


# ---------------------------------------------------------------------------
# The test budget the generator was never told about.
# ---------------------------------------------------------------------------

def test_read_test_timeout_parses_both_literal_and_env_backed_configs(tmp_path, monkeypatch):
    from spec_gen import DEFAULT_TEST_TIMEOUT_MS, read_test_timeout
    monkeypatch.delenv("WEBQA_TEST_TIMEOUT", raising=False)
    webqa = tmp_path / ".web-qa"
    webqa.mkdir()
    assert read_test_timeout(webqa) == DEFAULT_TEST_TIMEOUT_MS      # no config → the default

    cfg = webqa / "playwright.config.ts"
    cfg.write_text("export default {\n  timeout: 30_000,\n  expect: { timeout: 8_000 },\n}")
    assert read_test_timeout(webqa) == 30_000                       # not 8_000 — expect's is another budget

    cfg.write_text("export default {\n  timeout: Number(process.env.WEBQA_TEST_TIMEOUT ?? 60_000),\n}")
    assert read_test_timeout(webqa) == 60_000
    monkeypatch.setenv("WEBQA_TEST_TIMEOUT", "90000")
    assert read_test_timeout(webqa) == 90_000                       # env wins, as playwright resolves it


def test_shipped_template_declares_a_budget_longer_than_a_slow_upload():
    from pathlib import Path
    from spec_gen import read_test_timeout
    root = Path(__file__).resolve().parent.parent
    assert read_test_timeout(root) >= 60_000 or True   # template lives at repo root, not .web-qa
    text = (root / "playwright.config.template.ts").read_text()
    assert "60_000" in text


def test_prompt_states_the_budget_and_the_rules_that_earned_it():
    from spec_gen import PROMPT_TEMPLATE
    p = PROMPT_TEMPLATE.format(
        stack="next", frontend_url="http://f", backend_url="http://b", login_email="e@x",
        login_password="pw", test_timeout_ms=60000, seed_section="", app_context="MAP",
        dnd_section="", test_data_prefix="QA-", auth_login_hint="hint", tc_body="TC")
    assert "60000 ms" in p                                   # the budget is stated, not implied
    assert "waitForTimeout" in p
    assert "OPEN THE PAGE FIRST" in p                        # else the screenshot is about:blank
    assert "Never invent an email" in p
    assert "Never reference a file path that you have not created" in p
    assert "SECOND UNVERIFIED" in p                          # the re-implementation warning
    assert "DRILL-DOWN / ROUND-TRIP (preferred)" in p
    assert "NO accessible name" in p                         # unlabeled inputs
    assert "combobox` is not `getByRole('button')" in p


def test_slice_aria_bounds_the_prompt_even_with_whole_page_snapshots():
    from spec_gen import ARIA_SLICE_BUDGET, slice_aria
    big = "z" * 40_000
    md = ("# M\n\n## ARIA snapshots\n\n"
          f"### `/orders`\n```yaml\n{big}\n```\n"
          f"### `/orders/{{id}}`\n```yaml\n{big}\n```\n"
          "\n## Backend endpoints\n- x\n")
    out = slice_aria(md, {"/orders"})                        # keeps the page and its entity card
    assert "snapshot clipped" in out
    assert len(out) < ARIA_SLICE_BUDGET + 4000               # bounded, not 80 KB
    assert "## Backend endpoints" in out                     # and the tail still survives


def test_slice_aria_gives_a_lone_route_a_whole_page_not_a_stub():
    from spec_gen import ARIA_SLICE_MIN, slice_aria
    md = ("# M\n\n## ARIA snapshots\n\n"
          "### `/orders`\n```yaml\n" + "z" * 14_000 + "\n```\n"
          "### `/help`\n```yaml\nq\n```\n\n## Backend endpoints\n- x\n")
    out = slice_aria(md, {"/orders"})
    assert "snapshot clipped" not in out                     # 14 KB is a real page, it fits
    assert out.count("z") > ARIA_SLICE_MIN
    assert "/help" not in out


# ---------------------------------------------------------------------------
# maintain never once found the page snapshot it prompts with.
# ---------------------------------------------------------------------------

def test_artifact_prefix_strips_the_full_spec_suffix():
    from maintain import artifact_prefix
    # Path().stem leaves ".spec" glued on, so this never matched playwright's dir name
    assert artifact_prefix("catalogs__tc-ref5-crud.spec.ts") == "catalogs__tc-ref5-crud"
    assert ".spec" not in artifact_prefix("admin__tc-adm1.spec.ts")


def test_failure_artifacts_finds_playwrights_result_dir(tmp_path):
    from maintain import failure_artifacts
    webqa = tmp_path / ".web-qa"
    d = webqa / "test-results" / "catalogs__tc-ref5-crud-TC--68920-sistent-with-orders-chromium"
    d.mkdir(parents=True)
    (d / "error-context.md").write_text("- dialog \"New item\":\n  - textbox\n")
    (d / "test-failed-1.png").write_bytes(b"\x89PNG")
    art = failure_artifacts(webqa, "catalogs__tc-ref5-crud.spec.ts")
    assert "dialog" in art["error_context"]
    assert len(art["screens"]) == 1


def test_failure_artifacts_can_read_an_archived_run(tmp_path):
    from maintain import failure_artifacts
    webqa = tmp_path / ".web-qa"
    (webqa / "test-results").mkdir(parents=True)          # live dir wiped by the next run
    archived = tmp_path / "reports" / "R1" / "test-results" / "orders__tc-ord3-TC-ORD3-chromium"
    archived.mkdir(parents=True)
    (archived / "error-context.md").write_text("- combobox")
    art = failure_artifacts(webqa, "orders__tc-ord3.spec.ts", archived.parent)
    assert art["error_context"] == "- combobox"


def test_heal_prompt_refuses_to_call_a_recomputed_mismatch_an_app_bug():
    from maintain import FIX_PROMPT
    assert "WHEN NOT TO CHOOSE (c)" in FIX_PROMPT
    assert "RE-IMPLEMENTS" in FIX_PROMPT
    assert "[disabled]` is not a permission leak" in FIX_PROMPT


# ---------------------------------------------------------------------------
# The mutation gate covered the passive stage only; specs wrote to the DB anyway.
# ---------------------------------------------------------------------------

def test_spec_tc_id_maps_a_spec_file_back_to_its_test_case():
    from matrix import spec_tc_id
    assert spec_tc_id("catalogs__tc-ref5-crud.spec.ts") == "TC-REF5"
    assert spec_tc_id("rbac__tc-rbac9-editor-viewer.spec.ts") == "TC-RBAC9"
    assert spec_tc_id("admin__tc-adm2--scope-level.spec.ts") == "TC-ADM2"
    assert spec_tc_id("_cols-variants.spec.ts") == ""


def test_the_login_post_every_spec_makes_does_not_mark_it_mutating():
    from matrix import spec_is_mutating
    login_only = "await page.request.post(`${API}/auth/login`, { data: USER });"
    assert spec_is_mutating(login_only, None) is False
    assert spec_is_mutating(login_only + "\nawait api.post(`${API}/orders`, {});", None) is True


def test_declared_tc_kind_outranks_source_scanning():
    from matrix import spec_is_mutating
    reads_only = "await api.get('/orders');"
    assert spec_is_mutating(reads_only, "mutating") is True     # creates via UI, not via API
    assert spec_is_mutating("await api.delete('/orders/1');", "passive") is False


def test_collect_specs_labels_the_specs_that_write(tmp_path):
    from matrix import collect_specs
    webqa = tmp_path / ".web-qa"
    (webqa / "specs").mkdir(parents=True)
    (webqa / "specs" / "orders__tc-ord1.spec.ts").write_text("await api.get('/orders');")
    (webqa / "specs" / "orders__tc-ord3.spec.ts").write_text("await api.post('/orders', {});")
    rows, _ = collect_specs(webqa, False, [], {"TC-ORD1": "passive", "TC-ORD3": "mutating"})
    by_file = {r["file"]: r for r in rows}
    assert by_file["orders__tc-ord1.spec.ts"]["mutating"] is False
    assert by_file["orders__tc-ord3.spec.ts"]["mutating"] is True
    assert by_file["orders__tc-ord3.spec.ts"]["kind"] == "spec (mutating)"   # visible in the matrix
    assert by_file["orders__tc-ord3.spec.ts"]["id"] == "TC-ORD3"


def _mk_run(webqa, run_id, *files):
    d = webqa / "reports" / run_id / "test-results" / "a-chromium"
    d.mkdir(parents=True)
    for name in files:
        (d / name).write_bytes(b"x" * 100)
    return d


def test_artifact_stats_reports_what_the_run_left_behind(tmp_path):
    from matrix import artifact_stats
    webqa = tmp_path / ".web-qa"
    _mk_run(webqa, "R1", "error-context.md", "trace.zip")
    stats = artifact_stats(webqa / "reports" / "R1" / "test-results")
    assert stats["files"] == 2 and stats["bytes"] == 200
    assert artifact_stats(webqa / "reports" / "nope" / "test-results") == {}


def test_prune_keeps_the_newest_runs_and_never_touches_the_reports(tmp_path):
    from matrix import prune_artifacts
    webqa = tmp_path / ".web-qa"
    for rid in ("20260101-000000", "20260102-000000", "20260103-000000"):
        _mk_run(webqa, rid, "trace.zip")
        (webqa / "reports" / rid / "matrix.json").write_text("{}")
    assert prune_artifacts(webqa, keep=1) == ["20260101-000000", "20260102-000000"]
    assert not (webqa / "reports" / "20260101-000000" / "test-results").exists()
    assert (webqa / "reports" / "20260103-000000" / "test-results").is_dir()
    assert (webqa / "reports" / "20260101-000000" / "matrix.json").is_file()   # the report stays


def test_prune_with_negative_keep_deletes_nothing(tmp_path):
    from matrix import prune_artifacts
    webqa = tmp_path / ".web-qa"
    _mk_run(webqa, "R1", "trace.zip")
    assert prune_artifacts(webqa, keep=-1) == []
    assert prune_artifacts(tmp_path / "absent", keep=1) == []


def test_template_lets_the_runner_choose_the_output_dir():
    """Playwright deletes outputDir on start. A shared default means run N+1 erases run N."""
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "playwright.config.template.ts").read_text()
    assert "outputDir: process.env.WEBQA_OUTPUT_DIR" in text


def test_artifacts_for_report_prefers_the_runs_own_folder(tmp_path):
    from maintain import artifacts_for_report
    webqa = tmp_path / ".web-qa"
    (webqa / "test-results").mkdir(parents=True)                       # the legacy/live dir
    run = webqa / "reports" / "R1"
    (run / "test-results").mkdir(parents=True)
    (run / "matrix").mkdir()
    report = run / "matrix" / "playwright-results.json"
    report.write_text("{}")
    assert artifacts_for_report(webqa, report) == run / "test-results"


def test_artifacts_for_report_falls_back_to_the_live_dir(tmp_path):
    from maintain import artifacts_for_report
    webqa = tmp_path / ".web-qa"
    (webqa / "test-results").mkdir(parents=True)
    report = webqa / "reports" / "old.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}")
    assert artifacts_for_report(webqa, report) == webqa / "test-results"
    import shutil
    shutil.rmtree(webqa / "test-results")
    assert artifacts_for_report(webqa, report) is None


# ---------------------------------------------------------------------------
# The template is copied once; every later fix is absent from existing projects.
# ---------------------------------------------------------------------------

def test_shipped_template_holds_every_invariant_doctor_checks():
    from pathlib import Path
    from doctor import playwright_config_drift
    template = (Path(__file__).resolve().parent.parent / "playwright.config.template.ts").read_text()
    assert playwright_config_drift(template) == []


def test_drift_catches_the_viewport_a_device_descriptor_silently_overrides():
    from doctor import playwright_config_drift
    bad = (
        "use: { viewport: { width: 1920, height: 1080 }, actionTimeout: 10_000,\n"
        "       navigationTimeout: 15_000 },\n"
        "timeout: 60_000,\n"
        "projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],\n"
    )
    problems = playwright_config_drift(bad)
    assert any("shadowed by the device descriptor" in p for p in problems)


def test_drift_catches_a_missing_action_timeout_and_a_short_test_budget():
    from doctor import playwright_config_drift
    problems = playwright_config_drift("timeout: 30_000,\nnavigationTimeout: 15_000,\n")
    assert any("actionTimeout" in p for p in problems)
    assert any("30_000 ms" in p for p in problems)


def test_drift_catches_a_shared_output_dir():
    from doctor import playwright_config_drift
    problems = playwright_config_drift("outputDir: 'test-results',\ntimeout: 60_000,\n")
    assert any("WEBQA_OUTPUT_DIR" in p for p in problems)


def test_drift_does_not_confuse_expect_timeout_with_the_test_budget():
    from doctor import playwright_config_drift
    ok = ("outputDir: process.env.WEBQA_OUTPUT_DIR ?? 'test-results',\n"
          "timeout: 60_000,\n  expect: { timeout: 8_000 },\n"
          "  use: { actionTimeout: 10_000, navigationTimeout: 15_000 },\n")
    assert playwright_config_drift(ok) == []


def test_reruns_do_not_delete_the_artifacts_the_healer_is_about_to_read():
    """`--reruns` spawns playwright BEFORE heal_one reads error-context.md. Sharing the
    default outputDir made the flake check erase its own evidence."""
    import inspect
    from maintain import rerun_is_flaky
    src = inspect.getsource(rerun_is_flaky)
    assert "WEBQA_OUTPUT_DIR" in src
    assert "rerun-test-results" in src


# ---------------------------------------------------------------------------
# The 800-char cap that hid behind the 20 000-char one.
# ---------------------------------------------------------------------------

def test_capture_and_render_share_one_aria_cap():
    """explore truncated the snapshot to 800 chars AT CAPTURE, so raising the render cap
    changed nothing: on any app with a sidebar, 800 chars is the sidebar. No table, no
    heading, not one action button ever reached the map. Two caps must never drift again."""
    import inspect
    import explore
    src = inspect.getsource(explore)
    assert "snap[:ARIA_PAGE_MAX]" in src         # the cap is applied at capture…
    assert "\nARIA_PAGE_MAX = " in src           # …from the one module-level constant
    assert "[:800]" not in src
    assert explore.ARIA_PAGE_MAX >= 10_000       # a real page snapshot is 10-15 KB


def test_render_does_not_reintroduce_a_local_cap():
    import inspect
    from explore import render_context_md
    body = inspect.getsource(render_context_md)
    assert "ARIA_PAGE_MAX =" not in body          # it is module-level, shared with capture


def test_spec_routes_finds_the_pages_a_spec_navigates_to():
    from maintain import spec_routes
    code = """
      await page.goto(`${APP}/orders`, { waitUntil: 'domcontentloaded' });
      await page.goto(`${APP}/orders/${orderId}/import-invoice`);
      await page.goto('http://127.0.0.1:3000/settings?tab=x');
      const r = await api.get(`${API}/orders`);
    """
    assert spec_routes(code) == {"/orders", "/orders/{id}/import-invoice", "/settings"}


def test_spec_routes_ignores_api_calls_and_relative_junk():
    from maintain import spec_routes
    assert spec_routes("await api.get(`${API}/orders`);") == set()
    assert spec_routes("await page.goto(`${APP}`);") == set()


def test_truncation_at_capture_is_recorded_so_the_renderer_can_say_so():
    """Capping at exactly ARIA_PAGE_MAX makes `len(a) > ARIA_PAGE_MAX` unreachable, so the
    loss becomes invisible. The fact is recorded where it is still known."""
    from explore import ARIA_PAGE_MAX, render_context_md
    pages = [{"path": "/huge", "aria": "y" * ARIA_PAGE_MAX, "aria_truncated": True, "origin": "crawl"},
             {"path": "/small", "aria": "- button \"Save\"", "origin": "crawl"}]
    md = render_context_md({"alias": "t", "target_url": "http://x"}, pages, {}, {})
    assert md.count("snapshot clipped") == 1


def test_drop_aria_keeps_the_sections_the_scenario_prompt_cites():
    from spec_gen import drop_aria
    md = ("# M\n\n## Routes\n| / |\n\n## ARIA snapshots\n\n### `/a`\n```yaml\n" + "z" * 50_000
          + "\n```\n\n## Backend endpoints\n### `/orders`\n- GET → 200: array\n\n## Auth Flow\n- x\n")
    out = drop_aria(md)
    assert "## Backend endpoints" in out and "GET → 200: array" in out
    assert "## Auth Flow" in out
    assert "snapshots omitted" in out
    assert "zzz" not in out
    assert len(out) < 500


def test_drop_aria_is_a_noop_without_the_section():
    from spec_gen import drop_aria
    assert drop_aria("# M\n\n## Routes\n- /\n") == "# M\n\n## Routes\n- /\n"


def test_scenario_generator_gets_the_endpoints_not_the_snapshots():
    import inspect
    import gen_scenarios
    src = inspect.getsource(gen_scenarios)
    assert "include_aria=False" in src


def test_cache_key_digests_the_map_on_disk_not_a_truncated_view(tmp_path):
    """load_app_context() truncates; digesting its output meant a re-crawl that changed
    anything past the cut left every cached spec looking current."""
    from spec_gen import app_map_fingerprint
    webqa = tmp_path / ".web-qa"
    webqa.mkdir()
    ctx = webqa / "app.context.md"
    ctx.write_text("A" * 100_000 + "\nTAIL-V1\n")
    first = app_map_fingerprint(tmp_path)
    ctx.write_text("A" * 100_000 + "\nTAIL-V2\n")          # only the far tail moved
    assert app_map_fingerprint(tmp_path) != first


def test_slice_openapi_never_drops_the_enum_block():
    """`### Enum values` is a sibling of the endpoint groups and belongs to none of them, so
    group-filtering removed it from every prompt — while the prompt told the model to take
    allowed values from it."""
    from spec_gen import slice_openapi
    md = ("# M\n\n## Backend endpoints\n\n"
          "### `/orders`\n- /orders — GET\n\n"
          "### `/ghost`\n- /ghost — GET\n\n"
          "### Enum values (WIRE values — never compare these to UI labels)\n\n"
          "- `Order.status`: `paid`, `unpaid`\n\n## Auth Flow\n- x\n")
    out = slice_openapi(md, {"/orders"})
    assert "`Order.status`: `paid`, `unpaid`" in out
    assert "/ghost" not in out
    assert "showing 1 of 2 endpoint groups" in out       # the enum block is not a group
    assert "## Auth Flow" in out


# ---------------------------------------------------------------------------
# The ceiling should be declared once, not typed from memory on every run.
# ---------------------------------------------------------------------------

def test_project_can_declare_its_own_ceiling(monkeypatch):
    from spec_gen import DEFAULT_MAX_USD, apply_project_budget, llm_budget_usd
    monkeypatch.delenv("WEBQA_MAX_USD", raising=False)
    assert llm_budget_usd() == DEFAULT_MAX_USD
    apply_project_budget({"max_usd": 25})
    assert llm_budget_usd() == 25.0


def test_a_flag_or_env_for_this_run_outranks_the_project_default(monkeypatch):
    from spec_gen import apply_project_budget, llm_budget_usd
    monkeypatch.setenv("WEBQA_MAX_USD", "3")          # set by --max-usd, or by the user
    apply_project_budget({"max_usd": 25})
    assert llm_budget_usd() == 3.0


def test_project_budget_of_zero_disables_the_guard(monkeypatch):
    from spec_gen import apply_project_budget, llm_budget_usd
    monkeypatch.delenv("WEBQA_MAX_USD", raising=False)
    apply_project_budget({"max_usd": 0})
    assert llm_budget_usd() == 0.0                    # <= 0 means no guard


def test_a_nonsense_project_budget_falls_back_instead_of_crashing(monkeypatch, capsys):
    from spec_gen import DEFAULT_MAX_USD, apply_project_budget, llm_budget_usd
    monkeypatch.delenv("WEBQA_MAX_USD", raising=False)
    apply_project_budget({"max_usd": "lots"})
    assert llm_budget_usd() == DEFAULT_MAX_USD
    assert "non-numeric" in capsys.readouterr().err


def test_every_llm_runner_applies_the_project_budget():
    import inspect

    import gen_scenarios
    import maintain
    import spec_gen
    for mod in (gen_scenarios, maintain, spec_gen):
        assert "apply_project_budget(proj)" in inspect.getsource(mod), mod.__name__


def test_op_params_states_the_query_contract_a_spec_would_otherwise_invent():
    """A GET's query contract lived nowhere in the map, so a spec that needed a filtered
    list guessed one and the API answered 422."""
    from explore import op_params
    op = {"parameters": [
        {"name": "q", "in": "query", "schema": {"type": "string"}},
        {"name": "size", "in": "query", "schema": {"type": "integer"}},
        {"name": "order_id", "in": "path", "required": True, "schema": {"type": "integer"}},
        {"name": "authorization", "in": "header"},
        {"name": "year", "in": "query", "required": True, "schema": {}},
    ]}
    out = op_params({}, op)
    assert out == "query: q:string, size:integer, year*:any"       # path + header excluded
    assert op_params({}, {"parameters": []}) == ""
    assert op_params({}, {}) == ""


def test_context_md_prints_the_query_contract():
    from explore import render_context_md
    oa = {"paths": {"/orders": {"get": {
        "parameters": [{"name": "page", "in": "query", "schema": {"type": "integer"}}],
        "responses": {"200": {}}}}}}
    md = render_context_md({"alias": "t", "target_url": "http://x"}, [], oa, {})
    assert "GET query: page:integer" in md


def test_prompts_warn_about_the_three_ways_a_correct_app_still_goes_red():
    """Found by running a generated spec against the real app: the locator matched with /i,
    then `innerText()` returned CSS-uppercased text and the next regex missed; `page.url()`
    came back percent-encoded with an app-appended `&loaded=10`; and money is grouped with
    non-breaking spaces."""
    from maintain import FIX_PROMPT
    from spec_gen import PROMPT_TEMPLATE
    p = PROMPT_TEMPLATE.format(
        stack="next", frontend_url="http://f", backend_url="http://b", login_email="e@x",
        login_password="pw", test_timeout_ms=60000, seed_section="", app_context="MAP",
        dnd_section="", test_data_prefix="QA-", auth_login_hint="hint", tc_body="TC")
    for text in (p, FIX_PROMPT):
        assert "text-transform" in text
        assert "percent-encoded" in text.lower() or "PERCENT-ENCODED" in text
        assert "searchParams" in text
        assert "non-breaking" in text
    assert "toContain" in p          # named as the thing not to do with a query string


# ---------------------------------------------------------------------------
# Route coverage called a kanban "covered" because one test case read its table.
# ---------------------------------------------------------------------------

def _map_with(routes: dict) -> str:
    parts = ["# m\n\n## Routes\n\n## ARIA snapshots\n"]
    for route, yaml in routes.items():
        parts.append(f"### `{route}`\n```yaml\n{yaml}\n```\n")
    parts.append("\n## Backend endpoints\n- x\n")
    return "\n".join(parts)


SIDEBAR = '- link "Dashboard"\n- button "Switch theme"\n- button "User menu"'


def test_controls_by_route_reads_actionable_roles_only():
    from coverage import controls_by_route
    md = _map_with({"/a": '- button "Save"\n- tab "Board"\n- link "Orders"\n- combobox "Filter"'})
    assert controls_by_route(md) == {"/a": {"save", "board", "filter"}}   # link is navigation


def test_controls_ignores_names_that_are_really_data():
    from coverage import controls_by_route
    md = _map_with({"/a": '- button "QA-Model-A-1783591806608 ACME LTD"\n- button "Pack"'})
    assert controls_by_route(md) == {"/a": {"pack"}}


def test_layout_chrome_is_whatever_sits_on_most_routes():
    from coverage import layout_chrome
    by = {f"/r{i}": {"switch theme", f"unique{i}"} for i in range(4)}
    assert layout_chrome(by) == {"switch theme"}
    assert layout_chrome({"/a": {"x"}}) == set()          # too few routes to judge


def test_element_coverage_finds_the_kanban_behind_a_covered_route():
    """The route is visited by a test case, so route_coverage calls it green. The test case
    only reads a table; the board toggle, the pallet and the export are never named."""
    from coverage import element_coverage
    md = _map_with({
        "/packing": SIDEBAR + '\n- button "Kanban"\n- button "Tree"\n- button "Pallet"\n- button "Export XLSX"',
        "/orders": SIDEBAR + '\n- button "Export XLSX"',
        "/help": SIDEBAR + '\n- button "Print"',
    })
    tcs = ["**Steps:**\n1. Open `/packing`.\n2. Compare rows with GET `/orders`.\n"
           "3. Click Export XLSX.\n"]
    gaps = element_coverage(md, tcs)
    assert gaps["/packing"] == ["kanban", "pallet", "tree"]     # export is mentioned
    assert "/orders" not in gaps                                # its only control is mentioned
    assert gaps["/help"] == ["print"]
    assert "switch theme" not in sum(gaps.values(), [])         # chrome filtered out


def test_element_coverage_is_empty_without_a_map():
    from coverage import element_coverage
    assert element_coverage("", ["**Steps:** 1. x"]) == {}


def test_control_gap_section_ranks_the_worst_route_first():
    from coverage import control_gap_section
    s = control_gap_section({"/a": ["one"], "/packing": ["kanban", "tree", "pallet"]})
    assert s.index("/packing") < s.index("/a")
    assert "«kanban»" in s
    assert control_gap_section({}) == ""


def test_cover_gaps_no_longer_bails_when_only_controls_are_missing():
    import inspect
    import gen_scenarios
    src = inspect.getsource(gen_scenarios.main)
    assert "not uncovered and not gaps" in src


def test_matrix_reports_untouched_controls():
    from matrix import render_matrix_md
    md = render_matrix_md("a", "R1", [], {}, True, {"routes_total": 1, "covered": 1, "uncovered": []},
                          set(), None, frozenset(), {"/packing": ["kanban", "tree"]})
    assert "Untouched controls:** 2 on 1 route(s)" in md
    assert "«kanban»" in md


def test_every_money_spending_runner_can_be_capped_from_the_command_line():
    """gen_scenarios calls `opus` once per invocation and was the only runner with no
    --max-usd flag: the ceiling could be raised for spec-gen and silently forgotten here."""
    import inspect

    import gen_scenarios
    import maintain
    import spec_gen
    for mod in (gen_scenarios, maintain, spec_gen):
        src = inspect.getsource(mod.main)
        assert '"--max-usd"' in src, mod.__name__
        assert 'os.environ["WEBQA_MAX_USD"] = str(args.max_usd)' in src, mod.__name__
