"""Running subprocesses, and stopping without leaving them behind."""

from __future__ import annotations

import contextlib
import signal
import subprocess
import threading
from typing import TYPE_CHECKING, Any

from .config import REPO_ROOT

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def run(
    cmd: list[str], cwd: Path | None = None, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """Run a command with sensible defaults."""
    return subprocess.run(
        cmd,
        cwd=cwd if cwd is not None else REPO_ROOT,
        capture_output=kwargs.pop("capture_output", True),
        text=True,
        # A repository's own bytes decide this, not the ambient locale: under
        # LC_ALL=C a single en dash in a source file made `git diff` raise.
        encoding="utf-8",
        errors="replace",
        check=False,
        **kwargs,
    )


#: The signals that end a run from outside: a kill, a closed terminal or ssh session
#: (the loop can run for half an hour), a Ctrl-\. SIGINT already unwinds, as
#: KeyboardInterrupt.
UNWIND_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)


@contextlib.contextmanager
def signals_unwind() -> Iterator[None]:
    """Inside the block, ``UNWIND_SIGNALS`` exit Python with cleanup, not without it.

    By default each ends Python without running ``finally`` blocks, context-manager
    exits or atexit. That would leave reviewers running, and copies of agy's sign-in
    token on disk. So each is raised as ``SystemExit(128 + signum)``, and the stack
    unwinds. One the caller ignores (``nohup``'s SIGHUP) stays ignored. The previous
    handlers are restored on the way out. Only the main thread can install a
    handler; anywhere else this does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    # A signal already ignored stays ignored: that is how ``nohup`` keeps a long run
    # alive through a closed terminal, and turning its SIGHUP into an exit undid it.
    previous = {
        sig: signal.signal(sig, _exit_on_signal)
        for sig in UNWIND_SIGNALS
        if signal.getsignal(sig) is not signal.SIG_IGN
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            # None: a handler installed outside Python, which cannot be put back.
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)


def _exit_on_signal(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def signals_deferred() -> Iterator[None]:
    """Inside the block, Ctrl-C and ``UNWIND_SIGNALS`` wait until it ends.

    For cleanup that is already under way: a second Ctrl-C or SIGTERM -- a user
    pressing it again, a supervisor's follow-up -- raised in the middle of it, and the
    reviewers not yet stopped kept running. One that arrives is delivered on the way
    out, and ends the run as before. Only where the platform can block signals, and
    from the main thread.
    """
    if (
        not hasattr(signal, "pthread_sigmask")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return
    held = {signal.SIGINT, *UNWIND_SIGNALS}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, held)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
