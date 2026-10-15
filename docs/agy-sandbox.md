# agy's sandbox: what it closes, and what it does not

agy, the Antigravity CLI, cannot make itself read-only. It has no deny list like
claude's `--disallowedTools`, and `--sandbox` restricts terminal commands only.
Measured on agy 1.3.4, it wrote to a file *outside* its workspace by absolute path
with nothing denied. Its plan mode never wrote in testing, but that is the model
choosing not to, not agy refusing.

So the boundary is the operating system's: agy runs under
[bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`), Linux only. Under
it, agy's own write tool got `read-only file system`. The wrapper is built in one
place, `agy_wrapper_parts` in `scripts/review_loop/agy_sandbox.py`, and both the
convergence loop and the full-mode runner call it.

## What it closes

- **Writes that outlive the sandbox**, anywhere but two throwaway directories outside
  the repository: an empty workspace, and the copy of agy's state mounted over
  `~/.gemini/antigravity-cli`. Both are removed when the round ends; one that
  cannot be is named. The empty `/tmp`, `/run` and home mounts are writable inside,
  and vanish with the sandbox.
- **The home directory**, read whole: `$HOME`, and the account's own where `HOME`
  points elsewhere (CI, `sudo -E`). Both are replaced by empty ones, so there is no
  `~/.ssh`, no cloud credentials and no other tools' tokens. A home of `/`, or an
  account with no passwd entry, fails the check instead.
- **The repository under review, and this agent's checkout**, wherever they are,
  replaced by empty ones too: agy is sent the scrubbed diff, and has no need of the
  tree, `.env` files and all. The startup check proves it for the checkout.
- **`/tmp` and `/run`**, replaced by empty ones, and with them the sockets there:
  tmux's, X11's, the session and system D-Bus, `docker.sock`, snapd's. Only the DNS
  resolver's file is put back, not its directory, which holds a socket too.
- **agy's real state**, replaced by a throwaway copy holding its sign-in token, with
  its program directories (`bin`, `builtin`) mounted read-only beside it and nothing
  else of the real state: no history, no other settings. The copy is removed when
  the round ends, or when the run is stopped by Ctrl-C, `SIGTERM`, `SIGHUP` or
  `SIGQUIT`. Only a `SIGKILL` leaves it. A run started under `nohup` keeps ignoring
  `SIGHUP`.
- **The environment**, cleared to a short allowlist before `bwrap` starts. `bwrap`
  is PID 1 inside, and `/proc/1/environ` would otherwise hold every API key the run
  was started with. The values are not on `bwrap`'s command line either, which any
  local user can read.
- **Host processes** (`--unshare-pid`), and a terminal to inject input into
  (`--new-session`).
- **Privilege**: agy runs in a user namespace of its own with every capability
  dropped (`--unshare-user --cap-drop ALL`), checked: bwrap otherwise keeps all of
  root's, and a remount would undo every read-only mount. It does not run as root
  at all -- the check refuses -- since root inside still owns root's files
  (`/etc/shadow`, host keys) and can read them; in a CI container that runs as root,
  run the review as an unprivileged user.
- **Anything that runs code outside**:
  - every writable socket left reachable, refused by the check rather than looked
    up in a list (docker's, LXD's and Incus's are masked so that they do not cost
    the slot). The check asks the kernel (`sock_diag`, over netlink) for the
    sockets of the host's network namespace, each by the exact path it was bound
    at; if the kernel will not list them, or the list comes back interrupted, the
    check fails. A socket bound in another namespace (a container's), or whose
    directory was renamed after, is not found at the path that reaches it;
  - any abstract socket of your own that takes connections, whatever its name: a
    session bus (`dbus-launch`'s, IBus's, or one at an address of its own), or
    anything else that admits your user (on a kernel before 5.3, which does not
    say whose a socket is, the listener is asked). Root's system daemons and
    other users' abstract sockets check who connects, and are not counted;
  - an X server, whoever runs it, that admits your user without its cookie. Stock
    GNOME and Xwayland grant `SI:localuser`, and a program inside could type into
    a terminal outside. `xhost -si:localuser:$USER` closes that.
- **Nested namespaces**: no user namespace of agy's own inside (`--disable-userns`,
  where bwrap is 0.8 or later), in which it could map itself root and mount over
  the read-only binds; its own hostname and cgroup view.

## How it is proven

The wrapper is verified at startup with no model call, codex-style. Until it passes,
agy runs only as `agy --version` inside the wrapper being checked, not even to list
its models.

Before each launch, what the sandbox can reach is checked again: a loop runs for
half an hour, and a bus, socket or X server can appear after startup. The slot goes
to its next backend if anything fails.

A run opted in to Bedrock -- `.use-hermes` in the project, `--prefer-hermes`, or a
"hermes review", "bedrock review" or "isolated review" request in full mode -- does
not run agy at all, the check included, unless `REVIEW_USE_AGY=1` -- and even then
only where Hermes is not installed, since Hermes answers slot 3 when it is.

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
- **Reads of everything else.** Outside the homes, `/tmp`, `/run` and the
  repository under review, the filesystem is readable: another checkout kept in
  `/srv` or `/workspace`, say, and `/var/tmp`.
- **Sockets the list does not show where they are**, as above: in another network
  namespace, or moved since they were bound.
- **Full mode runs the project's own copy of these scripts**, the sandbox builder
  among them. Reviewing an untrusted branch that way runs that branch's code. Use the
  convergence loop from a trusted checkout of this agent instead, with `--repo`
  pointing at the branch. That keeps the sandbox, the scrubber and the prompts the
  checkout's own, not everything: the preflight runs the branch's test suite for its
  coverage figures -- from the project's installed copy of the preflight when it has
  one -- and the fix loop's gate runs its tests again. Run either only on a branch
  you would run the tests of; `--report-only` still runs the preflight.
- **agy's browser and MCP tools.** Headless agy refused the one shell command it
  tried, but whether those tools reach the network unprompted has not been tested.
- **Root's and other users' abstract sockets.** They are not counted: they usually
  check who connects, and counting them would switch the slot off on a stock Ubuntu
  host, where multipathd and iscsid keep two. A daemon that admits your user over
  one anyway is reachable from inside. `MMCRA_AGY_STRICT_SOCKETS=1` counts every
  abstract listener but the X servers (which are asked), at the cost of the slot on
  such a host.
- **Filesystems mounted after the sandbox starts.** A mount the host makes while agy
  runs -- removable media, an automount -- appears inside, and is not made read-only.
- **Being switched off by another local user.** The check fails closed, so any
  user who can leave a world-writable socket where the sandbox can see it
  (`/var/tmp`, say), or an X socket that never answers, turns the agy slot off
  for everyone; the message names the socket. The slot goes to its next backend.
- **Kernel keyrings.** They are not namespaced: keys this user owns -- Kerberos
  tickets, ecryptfs or fscrypt keys, anything `keyctl` stored -- are readable
  inside, as they are to any of the user's processes. Closing that takes a seccomp
  filter on the `keyctl` calls, which the wrapper does not install.
- **`@path` mentions in the prompt.** Whether agy reads a file named that way in
  stream-json input, and sends its contents to the model, has not been measured. A
  diff line of that shape is passed through as it is; the file would come from
  outside the scrubbed diff, though only from what the sandbox can read.

Treat the sandbox as stopping file writes, reads of your home directory and of the
repository under review, and code running outside it through local sockets. It is
not network isolation, and it does not hide the rest of the filesystem.

## If bwrap cannot start

The check then says `bwrap could not start`, with bwrap's own message. On Ubuntu
23.10 and later the usual cause is `kernel.apparmor_restrict_unprivileged_userns=1`,
which keeps bwrap from creating the user namespace it needs; codex's sandbox is
bwrap too, and docs/codex-sandbox.md has the fix (an AppArmor profile scoped to
bwrap, rather than turning the restriction off). Until then the slot goes to its
next backend.

## Switches

- `REVIEW_USE_AGY` set to anything but `1`, `true`, `yes` or `on` (`0`, empty, a
  typo): do not use agy. It is on by default wherever it is installed, and then sends
  the scrubbed diff, audit and context to Google. Every round that does so says so.
- `REVIEW_USE_AGY=1`: in a project pinned to Bedrock, let agy answer slot 3 when
  Hermes is not installed (with Hermes, Hermes answers it).
- `MMCRA_AGY_STRICT_SOCKETS=1`: refuse every abstract socket that takes connections,
  whoever owns it, not only your own (see above).
- `MMCRA_SKIP_AGY_SANDBOX_CHECK=1` (or `true`, `yes`, `on`): run agy without the
  wrapper, on a host that is already a sandbox. `0`, or any other value, keeps the
  sandbox.
