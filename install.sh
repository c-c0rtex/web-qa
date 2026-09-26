#!/usr/bin/env bash
# web-qa setup: uv-managed environment + chromium. The project registry is created by
# web-qa-register-project in ~/.config/web-qa/projects.json (or $WEBQA_REGISTRY), never here.
set -e
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

uv sync
uv run playwright install chromium

echo "web-qa installed."
echo "Next steps:"
echo "  1. bin/web-qa-register-project <alias> --target-url http://127.0.0.1:3000 [--backend-url ...]"
echo "  2. put credentials into ${WEBQA_REGISTRY:-${XDG_CONFIG_HOME:-$HOME/.config}/web-qa/projects.json} → \"auth\""
echo "  3. bin/web-qa-explore --alias <alias>"
