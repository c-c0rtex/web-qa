"""Shell lines for bin/web-qa-run-specs: the project's path and its spec environment.

Prints `PROJECT_PATH=…` and one `export K=V` per spec_env variable the caller has not set
already (a caller's value wins, as in export_spec_env). Meant for `eval`.
"""

from __future__ import annotations

import argparse
import os
import shlex

from explore import load_project, spec_env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    args = ap.parse_args()
    proj = load_project(args.alias)
    print(f"PROJECT_PATH={shlex.quote(proj['path'])}")
    for k, v in spec_env(proj).items():
        if k not in os.environ:
            print(f"export {k}={shlex.quote(v)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
