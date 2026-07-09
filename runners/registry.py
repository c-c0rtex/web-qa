"""Registry (projects.json) resolution — stdlib only, importable from bin/ scripts too.

Resolution order:
  1. WEBQA_REGISTRY env var (tests, CI, power users)
  2. $XDG_CONFIG_HOME/web-qa/projects.json (~/.config by default)

The registry holds `alias`, `path`, `auth` and `roles` — machine paths and credentials.
It deliberately does NOT live inside the skill: a plugin's cache directory changes on
every update, a git-clone copy shadows the user's real registry (a repo checkout would
answer "alias not in registry"), and neither is a place for passwords. Everything about
the project that is not a secret belongs in `<project>/.web-qa/config.json`.
"""

from __future__ import annotations

import os
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent


def _xdg_registry() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "web-qa" / "projects.json"


def registry_path() -> Path:
    """Registry to READ."""
    env = os.environ.get("WEBQA_REGISTRY")
    return Path(env) if env else _xdg_registry()


def registry_write_path() -> Path:
    """Registry to WRITE (register-project). Same location — there is only one."""
    env = os.environ.get("WEBQA_REGISTRY")
    return Path(env) if env else _xdg_registry()
