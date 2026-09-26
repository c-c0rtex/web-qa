"""Which version of its test case a generated spec was written for.

A spec's file name is `<scenario>__<slug(TC-ID + title)>.spec.ts`, and a title in a non-latin
script slugifies to nothing — the name then carries the id alone. After the scenarios were
regenerated, a spec written for one test case was run, and credited, as the spec of a NEW test
case that merely inherited its id; its pass also silenced that test case's own passive check.
The orphan rule looks for the title in the name, so it could not see this.

spec-gen records a signature of the test case each spec was generated from. A mismatch means
the spec is `stale`: not run, not credited, regenerate it. Dependency-free on purpose —
spec_gen, matrix and run_scenarios all import it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

SIG_FILE = ".signatures.json"


def tc_signature(tc: dict) -> str:
    text = f"{tc.get('title', '')}\n{tc.get('body', '')}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_signatures(specs_dir: Path) -> dict[str, str]:
    try:
        return json.loads((specs_dir / SIG_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def record_signatures(specs_dir: Path, sigs: dict[str, str | None]) -> None:
    """Merge `spec file name → signature` into the sidecar; a None value drops the entry."""
    if not sigs:
        return
    merged = load_signatures(specs_dir)
    for name, sig in sigs.items():
        if sig is None:
            merged.pop(name, None)
        else:
            merged[name] = sig
    (specs_dir / SIG_FILE).write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n",
                                      encoding="utf-8")


def is_stale(spec_name: str, tc: dict | None, sigs: dict[str, str]) -> bool:
    """True when the spec was generated for a DIFFERENT version of this test case.
    No record (a spec older than signatures) is not stale — unknown is not evidence."""
    recorded = sigs.get(spec_name)
    return bool(recorded and tc is not None and recorded != tc_signature(tc))
