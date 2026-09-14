# Making the codex reviewer genuinely read-only

This tool's central promise is that reviewers **report and never edit**. For the Claude and
Gemini CLIs that is a flag. For codex it depends on the host, and two unrelated things can
silently defeat it. Both were observed on a stock Ubuntu 24.04 machine, and each one on its
own turns a "read-only reviewer" into an agent with write access to the code under review.

## Fault 1: the sandbox cannot start

Ubuntu 23.10+ ships `kernel.apparmor_restrict_unprivileged_userns=1`, which stops
bubblewrap creating a user namespace:

```
bwrap: setting up uid map: Permission denied
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
```

Codex's sandbox never launches. **This is the dangerous part: a sandbox that fails to launch
looks identical from the outside to a sandbox that correctly refused a write.** In both cases
your canary file is untouched.

Fix, scoped to bubblewrap so the system-wide restriction stays in force for everything else:

```bash
sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF
sudo apparmor_parser -r /etc/apparmor.d/bwrap
```

Verify: `bwrap --unshare-all --ro-bind / / --dev /dev sh -c "echo ok"`

The blunt alternative, `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`,
disables the protection for every process on the machine. Prefer the profile.

## Fault 2: `approvals_reviewer = "auto_review"` overrides `-s read-only`

If `~/.codex/config.toml` contains `approvals_reviewer = "auto_review"`, codex auto-approves
the model's escalation requests "using the workspace-write sandbox" (codex's own wording in
`codex exec --help`). That overrides `-s read-only` entirely.

Verified side by side, codex-cli 0.153.4, same prompt, same repo:

```
codex exec -s read-only ...                               -> write SUCCEEDED
codex exec -s read-only -c approvals_reviewer="user" ...  -> "Read-only file system", refused
```

Note this is **not** about `trust_level`. The write also succeeded in a throwaway git repo
that had no trusted entry at all, so trust level is a red herring here.

This tool now pins `-c approvals_reviewer="user"` on every codex invocation, so a user's
global config can no longer widen a reviewer's powers. No action needed on your side.

## What the tool does about it

1. **Pins both flags.** Every `codex exec` gets `-s read-only` and
   `-c approvals_reviewer="user"`. Previously no sandbox flag was passed at all and the
   host default was trusted.
2. **Proves it, per run.** `detect_backends()` calls `codex_is_confined()`, which uses
   `codex sandbox` (no model call, so it is free) and checks three things in order:
   the sandbox **runs**, it can **read**, and a write is **refused**. All three are required.
   Checking only the last one is what makes a dead sandbox look safe.
3. **Drops the slot if it fails**, printing the reason and pointing here. An unconfined
   codex is worse than no codex, so the review continues with the remaining reviewers
   rather than quietly using an agent that can edit your code.

To bypass on a host that is already externally sandboxed (a disposable container, CI):

```bash
export MMCRA_SKIP_CODEX_SANDBOX_CHECK=1
```

## Checking by hand

```bash
d=$(mktemp -d); echo original > "$d/canary.txt"
codex sandbox -- sh -c "echo SANDBOX_RAN"            # 1. does it run?
codex sandbox -- sh -c "cat $d/canary.txt"           # 2. can it read?
codex sandbox -- sh -c "echo changed > $d/canary.txt"  # 3. is the write refused?
cat "$d/canary.txt"   # must still say "original", AND step 1 must have printed
```

Step 1 is not optional. Without it, a surviving canary proves nothing.
