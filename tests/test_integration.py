"""End-to-end pipeline tests against the fixture app — real chromium, zero LLM tokens.

Covers the realistic paths unit tests can't: explore → app map (aria, manual section),
the passive runner (statuses, a11y, role-skip, visual regression), the matrix inventory,
and spec-gen/maintain with a stubbed `claude` CLI.

Run: uv run pytest -m integration
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture_app import start  # noqa: E402

pytestmark = pytest.mark.integration

RUNNERS = Path(__file__).resolve().parent.parent / "runners"

STUB_CLAUDE = """#!/usr/bin/env python3
import os, sys
mode = os.environ.get("STUB_MODE", "spec")
if mode == "spec":
    print("import { test, expect } from '@playwright/test';\\n"
          "test('TC: stub', async ({ page }) => { expect(1).toBe(1); });")
elif mode == "transient":
    print("// TRANSIENT: dev server was down")
    print("import { test } from '@playwright/test';")
elif mode == "appbug":
    print("// APP-BUG: total ignores discount")
    print("import { test } from '@playwright/test';")
    print("test.fixme('TC: bug', async () => {});")
"""


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Fixture app + a registered temp project + isolated registry + stub claude."""
    srv, port = start()
    base = f"http://127.0.0.1:{port}"
    root = tmp_path_factory.mktemp("proj")
    webqa = root / ".web-qa"
    (webqa / "scenarios").mkdir(parents=True)
    (webqa / "specs").mkdir()
    registry = tmp_path_factory.mktemp("reg") / "projects.json"
    registry.write_text(json.dumps([{
        "alias": "fx", "path": str(root), "target_url": base, "backend_url": base,
        "auth": {"email": "admin@example.com", "password": "secret"},
        "roles": [{"name": "viewer", "email": "viewer@example.com", "password": "secret"}],
        "route_hints": [{"path": "/", "keywords": ["dashboard"]}],
    }]))
    stub_dir = tmp_path_factory.mktemp("stub")
    stub = stub_dir / "claude"
    stub.write_text(STUB_CLAUDE)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    e = dict(os.environ, WEBQA_REGISTRY=str(registry),
             PATH=f"{stub_dir}:{os.environ['PATH']}")
    yield {"base": base, "root": root, "webqa": webqa, "env": e}
    srv.shutdown()


def run_runner(name: str, args: list[str], env: dict) -> tuple[int, str, str]:
    proc = subprocess.run([sys.executable, str(RUNNERS / name), *args],
                          capture_output=True, text=True, timeout=300, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def last_json(stdout: str) -> dict:
    """Parse the trailing JSON object — runners print pretty (multi-line) summaries."""
    idx = stdout.rfind("\n{")
    return json.loads(stdout[idx + 1:] if idx != -1 else stdout)


def test_explore_builds_grounded_map_and_keeps_manual_notes(env):
    rc, out, err = run_runner("explore.py", ["--alias", "fx", "--max-pages", "5"], env["env"])
    assert rc == 0, err
    ctx = (env["webqa"] / "app.context.md").read_text()
    # routes, forms, aria, openapi schema all landed in the map
    assert "`/items`" in ctx
    assert "Create item" in ctx                      # real button label
    assert "ARIA snapshots" in ctx and "heading" in ctx
    assert "POST body: name*:string, price:integer" in ctx   # request schema
    assert "<!-- manual -->" in ctx                  # scaffolded marker
    # hand-written note survives a re-crawl
    (env["webqa"] / "app.context.md").write_text(
        ctx + "\nOnly admins may delete gadgets.\n")
    rc, out, err = run_runner("explore.py", ["--alias", "fx", "--max-pages", "5"], env["env"])
    assert rc == 0, err
    ctx2 = (env["webqa"] / "app.context.md").read_text()
    assert "Only admins may delete gadgets." in ctx2
    assert ctx2.count("<!-- manual -->") == 1


SCENARIO = """# Fixture regression

## TC-F1 — items table renders
**Type:** passive
**Steps:**
1. Open `/items`
2. Check GET `/health`
**Expected:**
- Items table with Name and Price columns
- Widget and Gadget rows visible
- Create item button present

## TC-F2 — viewer-only case
**Type:** passive
**Role:** viewer
**Steps:**
1. Open `/items`
**Expected:**
- Items table visible
"""


def test_passive_run_statuses_a11y_and_role_skip(env):
    (env["webqa"] / "scenarios" / "reg.md").write_text(SCENARIO)
    rc, out, err = run_runner("run_scenarios.py", ["--alias", "fx"], env["env"])
    results = json.loads(sorted((env["webqa"] / "reports").glob("*/results.json"))[-1].read_text())
    by_id = {r["id"]: r for r in results["results"]}
    assert by_id["TC-F1"]["status"] == "pass", by_id["TC-F1"]["notes"]
    assert by_id["TC-F2"]["status"] == "skip"                 # declared viewer, run as default
    assert "viewer" in by_id["TC-F2"]["notes"][0]
    # under --role viewer the declared TC actually runs
    rc, out, err = run_runner("run_scenarios.py", ["--alias", "fx", "--role", "viewer"], env["env"])
    results = json.loads(sorted((env["webqa"] / "reports").glob("*/results.json"))[-1].read_text())
    by_id = {r["id"]: r for r in results["results"]}
    assert by_id["TC-F2"]["status"] == "pass", by_id["TC-F2"]["notes"]


def test_a11y_violation_detected(env):
    scen = env["webqa"] / "scenarios" / "a11y.md"
    scen.write_text("# a11y\n\n## TC-A1 — dashboard\n**Type:** passive\n**Steps:**\n"
                    "1. Open `/`\n**Expected:**\n- Dashboard heading visible\n")
    rc, out, err = run_runner("run_scenarios.py",
                              ["--alias", "fx", "--scenarios", "a11y.md"], env["env"])
    results = json.loads(sorted((env["webqa"] / "reports").glob("*/results.json"))[-1].read_text())
    tc = results["results"][0]
    ids = {v["id"] for v in tc["a11y_critical"]}
    assert "image-alt" in ids                      # the deliberate <img> without alt


def test_visual_regression_fires_on_layout_change(env):
    (env["webqa"] / "scenarios" / "vis.md").write_text(
        "# vis\n\n## TC-V1 — dashboard layout\n**Type:** passive\n**Steps:**\n"
        "1. Open `/`\n**Expected:**\n- Dashboard heading visible\n")
    scen_args = ["--alias", "fx", "--scenarios", "vis.md"]
    rc, out, err = run_runner("run_scenarios.py", [*scen_args, "--update-baseline"], env["env"])
    assert rc == 0, err
    httpx.post(env["base"] + "/toggle-broken", timeout=5)     # layout change
    try:
        rc, out, err = run_runner("run_scenarios.py", scen_args, env["env"])
        results = json.loads(sorted((env["webqa"] / "reports").glob("*/results.json"))[-1].read_text())
        tc = results["results"][0]
        assert tc["status"] == "fail"
        assert any("VISUAL regression" in n for n in tc["notes"]), tc["notes"]
    finally:
        httpx.post(env["base"] + "/toggle-broken", timeout=5)


def test_matrix_inventory_and_coverage(env):
    rc, out, err = run_runner("matrix.py", ["--alias", "fx", "--list"], env["env"])
    assert rc == 0, err
    data = json.loads(out)
    assert data["scenario_tcs"] >= 3
    assert data["coverage"]["routes_total"] >= 2              # / and /items from the map
    # `/items` is referenced by TC text; `/` is only reachable via route_hints inference,
    # which coverage deliberately does not count — so it must show up as uncovered
    assert data["coverage"]["covered"] >= 1
    assert "/" in data["coverage"]["uncovered"]


def test_spec_gen_with_stub_llm_and_cache(env):
    (env["webqa"] / "scenarios" / "mut.md").write_text(
        "# mut\n\n## TC-M1 — create item\n**Type:** mutating\n**Steps:**\n"
        "1. Open `/items`\n2. Submit the create form\n**Expected:**\n- item created\n")
    e = dict(env["env"], STUB_MODE="spec")
    rc, out, err = run_runner("spec_gen.py", ["--alias", "fx"], e)
    assert rc == 0, err + out
    summary = last_json(out)
    assert any("TC-M1" in g for g in summary["generated"]), summary
    specs = list((env["webqa"] / "specs").glob("mut__*.spec.ts"))
    assert specs and "stub" in specs[0].read_text()
    # second run: cache hit, no regeneration
    rc, out, err = run_runner("spec_gen.py", ["--alias", "fx"], e)
    summary = last_json(out)
    assert any("TC-M1" in s for s in summary["skipped_cached"]), summary


def _failing_report(spec_name: str) -> dict:
    return {"suites": [{"specs": [{"file": f"specs/{spec_name}", "tests": [
        {"status": "unexpected", "results": [{"error": {"message": "locator not found"}}]}]}]}]}


def test_maintain_transient_leaves_spec_untouched(env):
    spec = env["webqa"] / "specs" / "t1.spec.ts"
    spec.write_text("original-content")
    report = env["webqa"] / "r1.json"
    report.write_text(json.dumps(_failing_report("t1.spec.ts")))
    e = dict(env["env"], STUB_MODE="transient")
    rc, out, err = run_runner("maintain.py",
                              ["--alias", "fx", "--report", str(report)], e)
    summary = last_json(out)
    assert summary["transient"] and summary["transient"][0]["spec"] == "t1.spec.ts"
    assert spec.read_text() == "original-content"             # untouched
    assert not spec.with_suffix(".spec.ts.proposed").exists()


def test_maintain_appbug_fixmes_and_records_bug(env):
    spec = env["webqa"] / "specs" / "t2.spec.ts"
    spec.write_text("original-content")
    report = env["webqa"] / "r2.json"
    report.write_text(json.dumps(_failing_report("t2.spec.ts")))
    e = dict(env["env"], STUB_MODE="appbug")
    rc, out, err = run_runner("maintain.py",
                              ["--alias", "fx", "--report", str(report)], e)
    summary = last_json(out)
    assert summary["app_bugs"] and "discount" in summary["app_bugs"][0]["bug"]
    proposed = spec.parent / "t2.spec.ts.proposed"
    assert proposed.exists() and "test.fixme" in proposed.read_text()
    assert spec.read_text() == "original-content"             # propose mode: original intact
    bugs = (env["webqa"] / "BUGS.md").read_text()
    assert "t2.spec.ts" in bugs and "discount" in bugs
