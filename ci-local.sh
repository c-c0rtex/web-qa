#!/usr/bin/env bash
# Mirror of .github/workflows/ci.yml — run BEFORE every push.
# (Doesn't validate action versions; check those with:
#  git ls-remote --tags https://github.com/<owner>/<action>)
set -euo pipefail
cd "$(dirname "$0")"

echo "== plugin manifests"
python3 -m json.tool .claude-plugin/plugin.json > /dev/null
python3 -m json.tool .claude-plugin/marketplace.json > /dev/null
if command -v claude > /dev/null; then
  claude plugin validate . --strict > /dev/null
fi

echo "== sync";        uv sync -q
echo "== lint";        uv run ruff check runners tests
echo "== unit";        uv run pytest -q
echo "== integration"; uv run pytest -m integration -q
echo "== cli smoke"
[ -f projects.json ] || cp projects.example.json projects.json
# the registry no longer lives in the skill root — point the runners at the local copy
export WEBQA_REGISTRY="$PWD/projects.json"
bin/web-qa-resolve-project --alias my-app > /dev/null
rc=0; bin/web-qa-matrix --alias my-app --list > /dev/null 2>&1 || rc=$?
test "$rc" -eq 2
echo "== template parses"
tmp=$(mktemp -d); mkdir -p "$tmp/.web-qa/specs"
cp playwright.config.template.ts "$tmp/.web-qa/playwright.config.ts"
printf '{"name":"web-qa-specs","private":true}\n' > "$tmp/.web-qa/package.json"
cat > "$tmp/.web-qa/specs/dummy.spec.ts" <<'TS'
import { test, expect } from '@playwright/test';
test('dummy', async () => { expect(1).toBe(1); });
TS
(cd "$tmp/.web-qa" && npm install -D @playwright/test --no-fund --no-audit --silent && npx playwright test --list > /dev/null)
rm -rf "$tmp"
echo "== ALL GREEN"
