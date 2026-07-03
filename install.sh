#!/usr/bin/env bash
# web-qa setup: uv-managed environment + chromium + projects.json scaffold.
set -e
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

uv sync
uv run playwright install chromium

[ -f projects.json ] || cp projects.example.json projects.json

echo "web-qa installed."
echo "Next steps:"
echo "  1. bin/web-qa-register-project <alias> --target-url http://127.0.0.1:3000 [--backend-url ...]"
echo "  2. put credentials into projects.json → \"auth\""
echo "  3. bin/web-qa-explore --alias <alias>"
