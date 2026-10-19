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
    """Run a command with sensible defaults -- among them no stdin, unless one is
    given: not the loop's own, which is usually the operator's terminal, for a
    command that waits on it (codex's sandbox check among them)."""
    if "input" not in kwargs:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
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
UNWIND_SIGNALS = tuple(
    getattr(signal, name)
    for name in ("SIGTERM", "SIGHUP", "SIGQUIT")
    if hasattr(signal, name)  # SIGHUP and SIGQUIT are Unix's
)


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


@contextlib.contextmanager
def signals_held() -> Iterator[None]:
    """Inside the block, Ctrl-C and ``UNWIND_SIGNALS`` are noted, not acted on; each is
    raised again on the way out.

    For starting a process and registering its cleanup as one step. Unlike
    ``signals_deferred`` it leaves the signal mask alone: a process started inside
    inherits the mask, and one started with SIGTERM blocked would never answer it. A
    signal the caller ignores stays ignored; only the main thread can do this.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    caught: list[int] = []

    def note(signum: int, _frame: object) -> None:
        caught.append(signum)

    previous = {
        sig: signal.signal(sig, note)
        for sig in (signal.SIGINT, *UNWIND_SIGNALS)
        if signal.getsignal(sig) is not signal.SIG_IGN
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)
        for sig in dict.fromkeys(caught):
            signal.raise_signal(sig)
