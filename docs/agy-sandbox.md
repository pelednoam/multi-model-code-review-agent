# agy's sandbox: what it closes, and what it does not

agy, the Antigravity CLI, cannot make itself read-only. It has no deny list like
claude's `--disallowedTools`, and `--sandbox` restricts terminal commands only.
Measured on agy 1.3.4, it wrote to a file *outside* its workspace by absolute path
with nothing denied. Its plan mode never wrote in testing, but that is the model
choosing not to, not agy refusing.

So the boundary is the operating system's: agy runs under
[bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`), Linux only. Under
it, agy's own write tool got `read-only file system`. The wrapper is built in one
place, `agy_sandbox_parts` in `scripts/review_loop/agy.py`, and both the
convergence loop and the full-mode runner call it.

## What it closes

- **Writes** anywhere but an empty temporary workspace outside the repository.
- **The home directory**, read whole: `$HOME`, and the account's own where `HOME`
  points elsewhere (CI, `sudo -E`). Both are replaced by empty ones, so there is no
  `~/.ssh`, no cloud credentials and no other tools' tokens. A home of `/`, or an
  account with no passwd entry, fails the check instead.
- **`/tmp` and `/run`**, replaced by empty ones, and with them the sockets there:
  tmux's, X11's, the session and system D-Bus, `docker.sock`, snapd's. Only the DNS
  resolver's directory is put back.
- **agy's real state**, replaced by a throwaway copy holding its sign-in token, with
  its program directories (`bin`, `builtin`) mounted read-only beside it and nothing
  else of the real state: no history, no other settings. The copy is removed when the round ends, or when the run is stopped by Ctrl-C,
  `SIGTERM`, `SIGHUP` or `SIGQUIT`. Only a `SIGKILL` leaves it. A run started under
  `nohup` keeps ignoring `SIGHUP`.
- **The environment**, cleared to a short allowlist before `bwrap` starts. `bwrap`
  is PID 1 inside, and `/proc/1/environ` would otherwise hold every API key the run
  was started with. The values are not on `bwrap`'s command line either, which any
  local user can read.
- **Host processes** (`--unshare-pid`), and a terminal to inject input into
  (`--new-session`).
- **Anything that runs code outside**:
  - every writable socket left reachable, refused by the check rather than looked
    up in a list (docker's, LXD's and Incus's are masked so that they do not cost
    the slot). The check reads `/proc/net/unix`, which lists the sockets of the
    host's network namespace by the path each was bound at. A socket bound in
    another namespace (a container's), or whose directory was renamed after, is
    not found at the path that reaches it; one whose name has a line break fails
    the check;
  - a session bus on an abstract socket (`dbus-launch`'s, IBus's);
  - an X server that admits your user without its cookie. Stock GNOME and
    Xwayland grant `SI:localuser`, and a program inside could type into a terminal
    outside. `xhost -si:localuser:$USER` closes that.

## How it is proven

The wrapper is verified at startup with no model call, codex-style. Until it passes,
agy runs only as `agy --version` inside the wrapper being checked, not even to list
its models.

Before each launch, what the sandbox can reach is checked again: a loop runs for
half an hour, and a bus, socket or X server can appear after startup. The slot goes
to its next backend if anything fails.

A project pinned to Bedrock (`.use-hermes`) does not run agy at all, the check
included, unless `REVIEW_USE_AGY=1`.

## What it does not close

- **The network**, which agy needs to reach its model. With it come:
  - services on localhost (docker's TCP API, databases, notebooks);
  - on a cloud VM or CI runner, the instance-metadata endpoint (`169.254.169.254`),
    which hands out the machine's cloud credentials;
  - abstract sockets other than a session bus or an X server.

  Do not run agy on a host whose instance role you would not give to the diff's
  author.
- **agy's own credentials.** Its sign-in token (the copy) and any proxy URL in the
  allowlisted environment are readable inside. With the network open, a diff that
  talks agy into a network-capable tool could send them out.
- **Reads outside the home directory and `/tmp`.** A repository kept elsewhere
  (`/srv`, `/workspace`, a CI checkout) is readable unscrubbed, and so is
  `/var/tmp`.
- **Sockets the list does not show where they are**, as above: in another network
  namespace, or moved since they were bound.
- **Full mode runs the project's own copy of these scripts**, the sandbox builder
  among them. Reviewing an untrusted branch that way runs that branch's code. Use the
  convergence loop from a trusted checkout of this agent instead, with `--repo`
  pointing at the branch.
- **agy's browser and MCP tools.** Headless agy refused the one shell command it
  tried, but whether those tools reach the network unprompted has not been tested.

Treat the sandbox as stopping file writes, reads of your home directory, and code
running outside it through local sockets. It is not network isolation, and it does
not hide a repository that lives outside the home directory.

## Switches

- `REVIEW_USE_AGY=0` (or `false`, `no`, `off`): do not use agy. It is on by default
  wherever it is installed, and then sends the scrubbed diff and audit to Google.
  Every round that does so says so.
- `REVIEW_USE_AGY=1`: use agy even in a project pinned to Bedrock.
- `MMCRA_SKIP_AGY_SANDBOX_CHECK=1` (or `true`, `yes`, `on`): run agy without the
  wrapper, on a host that is already a sandbox. `0`, or any other value, keeps the
  sandbox.
