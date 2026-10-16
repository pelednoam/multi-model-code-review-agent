"""What a claude reviewer may use: the deny list that goes with --allowedTools.

One copy, for the convergence loop's claude reviewers and the full-mode runner's
(``runner_cli claude-deny``).
"""

from __future__ import annotations

import contextlib
import os
import sys
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
    granted, scoped = _granted(tools)
    denied = [tool for tool in CLAUDE_DENIED if tool not in granted]
    return denied, [tool for tool in scoped if tool in CLAUDE_DENIED]


def claude_tool_names(tools: str) -> str:
    """The --tools value for a claude reviewer allowed ``tools``: the names it grants,
    comma-separated -- the only tools claude then has at all, a second layer under the
    deny list for any tool that list does not name. Empty, which is no tools, for a
    spec that grants nothing."""
    return ",".join(_granted(tools)[0])


def _granted(tools: str) -> tuple[list[str], list[str]]:
    """The tool names an --allowedTools spec grants, in order, and those granted with a
    scope. An unbalanced spec grants nothing."""
    granted: list[str] = []
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
            if name and name not in granted:
                granted.append(name)
            name = ""
        elif depth == 0:
            name += c
    if depth != 0:
        return [], []
    return granted, scoped


#: Where credentials live, which a claude reviewer is never allowed to read, whatever
#: it is granted: its reads are not confined to the repository (measured, it read
#: ~/.bashrc with only Read granted), and a diff that talks it into quoting one puts
#: it in a finding. A deny on Read covers Grep and Glob too (measured), and a symlink
#: to a denied file is refused as the file is (measured). A list, so not complete:
#: docs/ensemble-review.md says so.
SECRET_READS = (
    "~/.ssh/**", "~/.aws/**", "~/.azure/**", "~/.gnupg/**", "~/.kube/**",
    "~/.docker/**", "~/.config/gcloud/**", "~/.config/gh/**", "~/.netrc",
    "~/.git-credentials", "~/.npmrc", "~/.pypirc", "~/.password-store/**",
    "~/.local/share/keyrings/**", "~/.claude/**", "~/.codex/**", "~/.gemini/**",
    "~/.hermes/**",
    "~/.config/**/credentials*", "~/.pgpass", "~/.my.cnf", "~/.vault-token",
    "~/.cargo/credentials*", "~/.terraform.d/**", "~/.s3cfg", "~/.boto", "~/.oci/**",
    "~/.config/hub", "~/.cache/huggingface/**", "~/.huggingface/**", "~/.kaggle/**",
    "~/.config/rclone/**", "~/.config/op/**", "~/.config/containers/**",
    "//run/user/*/containers/**", "~/.config/doctl/**", "~/.config/hcloud/**",
    "~/.config/heroku/**", "//**/*.tfstate", "//**/*.tfstate.backup", "//**/*.tfvars",
    # Container and orchestrator secret mounts, and the host's private TLS keys.
    "//run/secrets/**", "//var/run/secrets/**", "//etc/ssl/private/**",
    "~/snap/**/.mozilla/**", "~/snap/**/.config/**", "~/.config/sops/**",
    "~/.config/age/**", "~/.age/**", "~/.yarnrc.yml", "//**/.yarnrc.yml",
    "~/.config/composer/auth.json", "~/.composer/auth.json", "//**/auth.json",
    "~/.nuget/**", "~/.ivy2/.credentials", "~/.local/share/fish/fish_history",
    "~/.node_repl_history", "~/.mysql_history", "~/.sqlite_history",
    # Browsers' and editors' profiles: cookies, saved sign-ins, extension tokens.
    "~/.config/google-chrome/**", "~/.config/chromium/**", "~/.mozilla/**",
    "~/.config/Code/**",
    # Where API keys are exported, and pasted into commands.
    "~/.bashrc", "~/.bash_profile", "~/.profile", "~/.zshrc", "~/.zshenv",
    "~/.zprofile", "~/.bash_history", "~/.zsh_history", "~/.python_history",
    "~/.psql_history",
    # The rest anywhere on the filesystem. "//" is an absolute path in a permission
    # rule, and a bare "**/" only the working directory (measured: a file in a sibling
    # directory was read under "**/", and refused under "//**/", as was one in the
    # working directory).
    # Environment files, the repository's included, and direnv's; joined, because the
    # diff scrubber rightly redacts a line that names one.
    "//**/." + "env", "//**/." + "env.*", "//**/." + "envrc",
    # The credential files above, in the repository under review or any other: a
    # remote's URL in .git/config can carry a token.
    "//**/.git/config", "//**/.git-credentials", "//**/.npmrc", "//**/.pypirc",
    "//**/.netrc", "//**/.docker/config.json",
    # What the diff scrubber holds to be a credential whole, by name or suffix
    # (scrub_diff.SECRET_NAMES, SECRET_SUFFIXES; a test keeps the two in step -- the
    # runner's private copy of this package has no scrubber to import). Joined where
    # the scrubber would redact the line.
    "//**/token", "//**/.htpasswd", "//**/credentials", "//**/id_rsa", "//**/id_dsa",
    "//**/id_ecdsa", "//**/id_ed25519", "//**/*.pem", "//**/*.key", "//**/*.p12",
    "//**/*.pfx", "//**/*.jks", "//**/*.keystore",
    # The CLIs' and build tools' own sign-ins.
    "~/.claude.json", "~/.config/github-copilot/**", "~/.m2/settings.xml",
    "~/.gradle/gradle.properties", "~/.config/pip/**", "~/.pip/**", "~/.terraformrc",
    # agy's throwaway copy of its sign-in token, wherever its scratch directory is, and
    # the loop's private directories for the reviewers' outputs and hermes's prompts.
    "//**/antigravity-oauth-token", "//**/mmcra-out-*/**", "//**/mmcra-prompt-*/**",
    "//**/mmcra-review-*/**",
    # All of /proc: processes' environments, this one's and the CLI's own among them,
    # and the aliases (/proc/self/root, /proc/<pid>/cwd) that reach every file above
    # by another path.
    "//proc/**",
)  # fmt: skip


def claude_disallowed(tools: str, repo: Path | None = None) -> list[str]:
    """The --disallowedTools entries for a claude reviewer allowed ``tools``: the
    tools of ``CLAUDE_DENIED`` it is not granted (``claude_denied``), and reads of
    ``SECRET_READS``, which no grant lifts -- but not of ``repo``, the repository
    under review, where one of those holds it (``_reads_around``)."""
    denied, _ = claude_denied(tools)
    reads = _secret_reads() if repo is None else _reads_around(_secret_reads(), repo)
    return [*denied, *(f"Read({path})" for path in reads)]


def around(ancestor: Path, target: Path) -> list[Path]:
    """What is in ``ancestor`` other than the way down to ``target``: at each level,
    the entries beside the one leading to it. Denying these instead of all of
    ``ancestor`` leaves ``target`` reachable, and the rest of ``ancestor`` denied."""
    out: list[Path] = []
    here = ancestor
    for part in target.relative_to(ancestor).parts:
        with contextlib.suppress(OSError):
            out += [entry for entry in here.iterdir() if entry.name != part]
        here = here / part
    return out


def _rule_dir(rule: str) -> Path | None:
    """The directory a ``.../**`` rule denies whole, or None for any other rule."""
    if not rule.endswith("/**") or any(c in rule[:-3] for c in "*?[]{}"):
        return None
    if rule.startswith("~/"):
        return Path(os.path.expanduser("~")) / rule[2:-3]
    if rule.startswith("//"):
        return Path(rule[1:-3])
    return None


def _reads_around(reads: list[str], repo: Path) -> list[str]:
    """``reads``, but each that denies a directory holding ``repo`` -- one kept under
    ~/.config, say -- replaced by what is around the way down to it: the reviewer is
    there to read the repository, and the rest stays denied."""
    target = repo.resolve()
    out: list[str] = []
    for rule in reads:
        base = _rule_dir(rule)
        if base is None or not target.is_relative_to(base.resolve()):
            out.append(rule)
            continue
        for entry in around(base.resolve(), target):
            if rule_safe(str(entry)):
                out += [f"/{entry}", f"/{entry}/**"]
    return out


def rule_safe(path: str) -> bool:
    """Whether ``path`` can go into a permission rule as itself: no whitespace, which
    separates rules, nor a comma, parenthesis or glob character, which the rule's own
    syntax reads."""
    return not any(c.isspace() or c in ",()*?[]{}" for c in path)


def _secret_reads() -> list[str]:
    """``SECRET_READS``, and its ``~/`` entries again under the account's own home
    (``with_account_home``)."""
    return with_account_home(SECRET_READS)


def with_account_home(paths: tuple[str, ...]) -> list[str]:
    """``paths``, and their ``~/`` entries again under the account's own home when
    $HOME names another directory (CI, ``sudo -E``, a wrapper): ``~`` in a rule is
    $HOME, and the files are in the account's."""
    reads = list(paths)
    try:
        import pwd

        account = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        return reads
    if not account or Path(account) == Path(os.path.expanduser("~")):
        return reads
    root = account.rstrip("/")
    if not rule_safe(root):
        # --disallowedTools takes its rules space-separated, with ( ) around a path and
        # globs in it: a rule with any of those in its path would mean something else.
        # To stderr: the runner reads stdout as the rules (``runner_cli claude-deny``).
        print(
            f"  WARNING: claude's credential denies do not cover {account!r}: its name",
            file=sys.stderr,
        )
        return reads
    return reads + [f"/{root}/{path[2:]}" for path in paths if path[:2] == "~/"]
