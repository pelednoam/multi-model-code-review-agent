"""What a claude reviewer may use: the deny list that goes with --allowedTools.

One copy, for the convergence loop's claude reviewers and the full-mode runner's
(``runner_cli claude-deny``).
"""

from __future__ import annotations

import os
from pathlib import Path

#: The tools a claude reviewer is denied unless it is granted them by name: each one
#: writes, runs a command, or reaches the network -- or, ReportFindings, takes the
#: review somewhere other than the JSON answer the loop reads: a reviewer handed it
#: reported through it, and answered in prose.
CLAUDE_DENIED = (
    "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "WebFetch", "WebSearch",
    "ReportFindings",
)  # fmt: skip


def claude_denied(tools: str) -> tuple[list[str], list[str]]:
    """What a claude reviewer allowed ``tools`` is denied, and what it is granted scoped.

    ``tools`` is an --allowedTools spec. That flag only skips the permission prompt;
    it does not confine. So every tool in ``CLAUDE_DENIED`` the spec does not name is
    denied -- a spec naming only reading tools must not lift the deny list with it.
    Only the names count, not what is inside a scope: ``Bash(git log --grep Edit:*)``
    names Bash, not Edit. An unbalanced spec grants nothing.

    The second list is the tools granted with a scope. A deny cannot keep a scope,
    which only the permission prompt enforces, and settings can turn that prompt off:
    the caller says so.
    """
    granted: set[str] = set()
    scoped: list[str] = []
    # One pass: ``depth`` counts open parentheses, and only text outside them is a
    # name -- ended by a separator, or marked scoped by the "(" that follows it.
    depth, name = 0, ""
    for c in tools + " ":
        if c == "(":
            if depth == 0 and name:
                scoped.append(name)
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                break
        elif depth == 0 and c in ", \t\n":
            granted.add(name)
            name = ""
        elif depth == 0:
            name += c
    if depth != 0:
        granted, scoped = set(), []
    denied = [tool for tool in CLAUDE_DENIED if tool not in granted]
    return denied, [tool for tool in scoped if tool in CLAUDE_DENIED]


#: Where credentials live, which a claude reviewer is never allowed to read, whatever
#: it is granted: its reads are not confined to the repository (measured, it read
#: ~/.bashrc with only Read granted), and a diff that talks it into quoting one puts
#: it in a finding. A deny on Read covers Grep and Glob too (measured). A list, so not
#: complete: docs/ensemble-review.md says so.
SECRET_READS = (
    "~/.ssh/**", "~/.aws/**", "~/.azure/**", "~/.gnupg/**", "~/.kube/**",
    "~/.docker/**", "~/.config/gcloud/**", "~/.config/gh/**", "~/.netrc",
    "~/.git-credentials", "~/.npmrc", "~/.pypirc", "~/.password-store/**",
    "~/.local/share/keyrings/**", "~/.claude/**", "~/.codex/**", "~/.gemini/**",
    "~/.config/**/credentials*", "~/.pgpass", "~/.my.cnf", "~/.vault-token",
    "~/.cargo/credentials*", "~/.terraform.d/**", "~/.s3cfg", "~/.boto", "~/.oci/**",
    "~/.config/hub",
    # Where API keys are exported, and pasted into commands.
    "~/.bashrc", "~/.bash_profile", "~/.profile", "~/.zshrc", "~/.zshenv",
    "~/.zprofile", "~/.bash_history", "~/.zsh_history", "~/.python_history",
    "~/.psql_history",
    # Environment files anywhere, the repository's included, and direnv's; joined,
    # because the diff scrubber rightly redacts a line that names one.
    "**/." + "env", "**/." + "env.*", "**/." + "envrc",
    # Processes' environments, this one's and the CLI's own with them. "//" is an
    # absolute path in a permission rule; a single "/" is relative to the settings.
    "//proc/*/environ", "//proc/self/environ",
)  # fmt: skip


def claude_disallowed(tools: str) -> list[str]:
    """The --disallowedTools entries for a claude reviewer allowed ``tools``: the
    tools of ``CLAUDE_DENIED`` it is not granted (``claude_denied``), and reads of
    ``SECRET_READS``, which no grant lifts."""
    denied, _ = claude_denied(tools)
    return [*denied, *(f"Read({path})" for path in _secret_reads())]


def _secret_reads() -> list[str]:
    """``SECRET_READS``, and its ``~/`` entries again under the account's own home
    when $HOME names another directory (CI, ``sudo -E``, a wrapper): ``~`` in a rule
    is $HOME, and the credentials are in the account's."""
    reads = list(SECRET_READS)
    try:
        import pwd

        account = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        return reads
    if not account or Path(account) == Path(os.path.expanduser("~")):
        return reads
    root = account.rstrip("/")
    return reads + [f"/{root}/{path[2:]}" for path in SECRET_READS if path[:2] == "~/"]
