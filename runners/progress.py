"""One readable line per step of work, on stderr, flushed.

A long run is a black box unless it says where it is. These runners each grew their own
ad-hoc prints, some on stdout — which is BLOCK-buffered the moment stdout is a pipe or a
file, so a `> run.log` swallowed progress in 8 KB chunks and the terminal showed nothing for
minutes. Progress belongs on stderr (line-buffered, and it survives `| jq` on the summary),
and it should always answer the same three questions:

    [gen] 14/72 · 4m12s · $3.84/$25.00 · OK  admin::TC-ADM7
     tag   where     how long   how much      what just happened

Nothing here knows about the LLM ledger; the spend callable is injected, so this module
imports nothing from the runners and cannot create a cycle.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable


def fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def emit(tag: str, msg: str) -> None:
    """A single progress line. stderr, flushed, never buffered away."""
    print(f"[{tag}] {msg}", file=sys.stderr, flush=True)


class Progress:
    """Counts steps of a known (or unknown) total and prints one line per step.

    `spend` returns `(spent_usd, budget_usd)`; the cost segment is omitted when it returns
    None or a zero budget, so a zero-token runner prints no misleading `$0.00`.

    Thread-safe: `begin()` is called from worker threads. Without it, a run with two workers
    printed only completions, so the second worker was invisible until it finished — the log
    looked like a serial run going at half speed.
    """

    def __init__(self, tag: str, total: int | None = None,
                 spend: Callable[[], tuple[float, float] | None] | None = None) -> None:
        self.tag = tag
        self.total = total
        self.spend = spend
        self.done = 0
        self.started = time.monotonic()
        self._inflight = 0
        self._lock = threading.Lock()

    # -- pieces -------------------------------------------------------------
    def _where(self) -> str:
        return f"{self.done}/{self.total}" if self.total else str(self.done)

    def _cost(self) -> str:
        if not self.spend:
            return ""
        got = self.spend()
        if not got:
            return ""
        spent, budget = got
        return f" · ${spent:.2f}/${budget:.2f}" if budget > 0 else f" · ${spent:.2f}"

    def elapsed(self) -> str:
        return fmt_elapsed(time.monotonic() - self.started)

    # -- api ----------------------------------------------------------------
    def start(self, msg: str) -> None:
        emit(self.tag, msg)

    def begin(self, what: str) -> None:
        """A unit of work was picked up. With one worker this is noise; with N it is the only
        way to see that N things are actually running."""
        with self._lock:
            self._inflight += 1
            n, where, el = self._inflight, self._where(), self.elapsed()
        busy = f"  ({n} in flight)" if n > 1 else ""
        emit(self.tag, f"{where} · {el} · ▶    {what}{busy}")

    def leave(self) -> None:
        """The worker is done with its unit — call from the WORKER, in a finally.

        Decrementing in `step()` instead counts wrong: a pool thread finishes one job and
        picks up the next before the main thread has harvested the future, so a 2-worker run
        cheerfully reported "3 in flight"."""
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def step(self, what: str, status: str = "OK") -> None:
        """One unit of work's RESULT was harvested. `status` is a short verdict: OK, FAIL…"""
        with self._lock:
            self.done += 1
            where, el = self._where(), self.elapsed()
        emit(self.tag, f"{where} · {el}{self._cost()} · {status:<4} {what}")

    def note(self, msg: str) -> None:
        """Something worth saying that is not a unit of work (a warning, a stage change)."""
        emit(self.tag, msg)

    def finish(self, msg: str = "") -> None:
        tail = f" · {msg}" if msg else ""
        emit(self.tag, f"done · {self._where()} · {self.elapsed()}{self._cost()}{tail}")
