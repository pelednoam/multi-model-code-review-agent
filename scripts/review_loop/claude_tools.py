"""What a claude reviewer may use: the deny list that goes with --allowedTools.

One copy, for the convergence loop's claude reviewers and the full-mode runner's
(``runner_cli claude-deny``).
"""

from __future__ import annotations


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
