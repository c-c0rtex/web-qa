"""Registry (projects.json) resolution — stdlib only, importable from bin/ scripts too.

Resolution order:
  1. WEBQA_REGISTRY env var (tests, CI, power users)
  2. <skill root>/projects.json — classic `git clone` install into ~/.claude/skills/web-qa
  3. $XDG_CONFIG_HOME/web-qa/projects.json (~/.config by default) — stable home for
     plugin installs: the plugin cache directory (~/.claude/plugins/cache/<id>/<version>/)
     changes on every update, so a registry stored there would vanish with each release.
"""

from __future__ import annotations

import os
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent


def _xdg_registry() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "web-qa" / "projects.json"


def registry_path() -> Path:
    """Registry to READ. Falls back to the skill-root path when nothing exists yet,
    so error messages point at the default location."""
    env = os.environ.get("WEBQA_REGISTRY")
    if env:
        return Path(env)
    local = SKILL_ROOT / "projects.json"
    if local.is_file():
        return local
    xdg = _xdg_registry()
    if xdg.is_file():
        return xdg
    return local


def registry_write_path() -> Path:
    """Registry to WRITE (register-project). An existing registry always wins; when
    creating fresh, a plugin install (version-scoped cache dir) gets the XDG path,
    a classic install keeps the skill root."""
    env = os.environ.get("WEBQA_REGISTRY")
    if env:
        return Path(env)
    local = SKILL_ROOT / "projects.json"
    if local.is_file():
        return local
    xdg = _xdg_registry()
    if xdg.is_file():
        return xdg
    if Path.home() / ".claude" / "plugins" in SKILL_ROOT.parents:
        return xdg
    return local
