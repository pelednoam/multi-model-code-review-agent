#!/usr/bin/env bash
# Full mode's reviewer runner (.claude/agents/ensemble-review.md, section 6): detects the backends,
# launches the four reviewers, waits for them, and turns their output into result-N.json.
#
#   bash scripts/run_full_review.sh REVIEW_TMP [--hermes]
#
# REVIEW_TMP: the round's directory, holding prompt-1.txt to prompt-4.txt (section 5). Everything
# this prints is also written to REVIEW_TMP/round.log, which ends "ROUND COMPLETE" once every
# result-N.json is written, and its exit status to REVIEW_TMP/runner-exit, however it ended.
# --hermes: the user asked for a Hermes/Bedrock review. A project pinned with .use-hermes is
# pinned whatever is passed.
#
# Run it as it is, in the background (run_in_background: true): which CLI takes each slot, and
# with which safety flags, is decided here, as the convergence loop decides it. A launcher written
# by hand drops those checks -- one that ran the gemini CLI for slot 3 skipped agy, its sandbox,
# and every credential deny the claude reviewers get.
#
# Launch, wait, and normalize - one shell, start to finish, so that `wait` sees the children it
# launched.
# All one group, closed by the exit at its end: bash reads a script as it runs it, and a reviewer
# with a file tool (hermes -t file --yolo) could rewrite what comes after the line being run. A
# group is read whole before any of it runs, and the exit stops bash reading past it.
{
set -uo pipefail
# bash 4.4 or later: `mapfile -d` reads agy's sandbox, and before 4.4 `set -u` failed on an empty
# array. macOS's /bin/bash is 3.2; install a newer one (Homebrew) to run this.
if [ "${BASH_VERSINFO[0]}" -lt 4 ] || { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -lt 4 ]; }; then
  echo "ERROR: the runner needs bash 4.4 or later (this is $BASH_VERSION)"; exit 1
fi
# With CDPATH set, `cd` to a relative path can go elsewhere and prints where it went, and every
# "$(cd DIR && pwd -P)" below would capture that line as part of the path.
unset CDPATH
USAGE="usage: bash scripts/run_full_review.sh REVIEW_TMP [--hermes]"
REVIEW_TMP="" HERMES_ASKED=false
for arg in "$@"; do
  case "$arg" in
    --hermes) HERMES_ASKED=true ;;
    -*)       echo "$USAGE"; exit 2 ;;
    *)        [ -z "$REVIEW_TMP" ] || { echo "$USAGE"; exit 2; }; REVIEW_TMP=$arg ;;
  esac
done
[ -n "$REVIEW_TMP" ] || { echo "$USAGE"; exit 2; }
# Absolute: the agy job changes into its workspace before its redirections are opened, and a
# relative REVIEW_TMP would then put its output inside a directory deleted after the run.
REVIEW_TMP="$(cd "$REVIEW_TMP" && pwd -P)" || { echo "ERROR: no directory $REVIEW_TMP"; exit 2; }
# The project this script is installed in, wherever it is run from: the claude and codex reviewers
# read the repository from their working directory.
SELF="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/$(basename -- "${BASH_SOURCE[0]}")" \
  || { echo "ERROR: cannot tell where this script is"; exit 1; }
REVIEW_ROOT="$(dirname -- "$(dirname -- "$SELF")")"
cd "$REVIEW_ROOT" || exit 1
INNER_ARGS=("$REVIEW_TMP")
! $HERMES_ASKED || INNER_ARGS+=(--hermes)
# A session of its own, so that "a group with a member in this session" (live_group) means one of
# this run's: in the caller's session, a group number reused by any other of its processes passed.
# The first runner starts the real one in a new session (setsid, which does not fork here: a
# background job is not a group leader), forwards it the signals that should stop it, and records
# what it printed (round.log) and how it ended (runner-exit). Without setsid, the real one runs in
# the caller's session, as a job of this one.
# Job control off first: with it on, the job below would lead a process group of its own, setsid
# would then fork, and $! be a parent that exits at once, leaving the run unwatched. (Not reproduced
# with util-linux's setsid under bash -m; off costs nothing, and set -m comes later, for the jobs.)
set +m
if [ -z "${MMCRA_OWN_SESSION:-}" ]; then
  export MMCRA_OWN_SESSION=1
  rm -f -- "$REVIEW_TMP/runner-exit"
  exec 3> >(tee "$REVIEW_TMP/round.log") || { echo "ERROR: cannot write $REVIEW_TMP/round.log"; exit 1; }
  tee_pid=$!
  # The trap first: set after the launch, a signal in between ended this shell and left the
  # real runner going. One that comes before there is a runner to forward it to is kept for it.
  inner="" pending=false
  trap 'pending=true; [ -z "$inner" ] || kill -TERM "$inner" 2>/dev/null' INT TERM HUP QUIT
  # This bash, checked above, not whichever comes first on PATH.
  if command -v setsid >/dev/null 2>&1; then
    setsid "$BASH" "$SELF" "${INNER_ARGS[@]}" >&3 2>&3 3>&- &
  else
    "$BASH" "$SELF" "${INNER_ARGS[@]}" >&3 2>&3 3>&- &
  fi
  inner=$!
  exec 3>&-
  ! $pending || kill -TERM "$inner" 2>/dev/null
  # Waited for at least once, to collect its status even if it has already exited; again while
  # it lives, since a forwarded signal ends a wait early.
  while :; do
    wait "$inner"; status=$?
    kill -0 "$inner" 2>/dev/null || break
  done
  # round.log whole before runner-exit says the run is over: tee ends once the last writer has
  # closed the pipe. Not waited for long -- a helper a reviewer left holding it is the sweep's.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$tee_pid" 2>/dev/null || break
    sleep 0.2
  done
  echo "$status" > "$REVIEW_TMP/runner-exit"
  exit "$status"
fi

# What bash cannot do -- agy's sandbox and input, taking a review out of a reviewer's output -- is
# the convergence loop's own code, called through one entry point (scripts/review_loop/runner_entry.py)
# rather than copied here. It comes from the project checkout, like everything this runner uses:
# reviewing an untrusted branch in full mode runs that branch's code. For that, run the convergence
# loop from a trusted checkout of this agent with --repo pointing at the branch.
# A private copy of that code and the result schema, taken before any reviewer starts: a reviewer
# with a file tool (hermes -t file --yolo) can write the checkout, and the runner goes on running
# this code -- extraction among it -- after the reviewers have.
# Links followed (-L): copied as links, a module could still be read from the checkout. A reviewer
# running as the same user could still find and rewrite this copy (hermes -t file --yolo can): it
# takes away the obvious path, not every path, as OUT_DIR does.
HELPER_ROOT=""
HELPER_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/mmcra-helper-XXXXXXXX")" && HELPER_ROOT="$(cd "$HELPER_ROOT" && pwd -P)" \
  && mkdir -p "$HELPER_ROOT/scripts" "$HELPER_ROOT/docs" \
  && cp -RL "$REVIEW_ROOT/scripts/review_loop" "$HELPER_ROOT/scripts/" \
  && { cp -L "$REVIEW_ROOT/docs/ensemble_review_result_schema.json" "$HELPER_ROOT/docs/" \
       || echo "WARNING: no result schema; agy's slot goes to its next backend"; } \
  || { echo "ERROR: could not copy the runner's helpers"; rm -rf -- "${HELPER_ROOT:-}"; exit 1; }
# Removed by finish_round from the moment it is set as the EXIT trap; until then, by this.
# OUT_DIR emptied first: one inherited from the environment (Cargo sets one) would be removed.
OUT_DIR=""
trap 'rm -rf -- "${HELPER_ROOT:-}" "${OUT_DIR:-}"' EXIT
review_helper() {
  python3 -I "$HELPER_ROOT/scripts/review_loop/runner_entry.py" "$@"
}

# JOB CONTROL IN A SCRIPT, deliberately. With `set -m` each background job gets its OWN process
# group, so `kill -- -$pid` reaches the whole tree. Without it every reviewer shares the runner's
# process group and can only be killed one process at a time - and a reviewer CLI is a tree (node
# spawning helpers), so killing the top of it leaves descendants running, still burning provider
# quota and still writing into $REVIEW_TMP after the report has been written. Verified: a reviewer
# whose child ignores SIGTERM survives the round entirely without this.
set -m

# OUT_DIR: where the reviewers write while they run -- stdout, stderr, codex's answer and the done
# markers -- private to this run, not $REVIEW_TMP. There, one reviewer with a file tool (hermes
# -t file --yolo) could write another slot's output while both ran, and answer for it or silence
# it. Copied into $REVIEW_TMP once every reviewer is stopped (publish_outputs), and read from
# there. A reviewer running as the same user could still find it: this takes away the obvious
# path, not every path. The done markers are also written to $REVIEW_TMP, for watching from
# another shell; the runner itself reads only its own.
# Absolute: the agy job changes into its workspace before its redirections are opened, and a
# relative TMPDIR would put them, and its done marker, somewhere else.
# Named as the loop names its own, mmcra-out-*: a name claude reviewers are denied reading.
OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mmcra-out-XXXXXXXX")" && [ -n "$OUT_DIR" ] && OUT_DIR="$(cd "$OUT_DIR" && pwd -P)" \
  || { echo "ERROR: no temporary directory"; exit 1; }
# mark_done N STATUS: slot N finished with STATUS.
mark_done() {
  echo "$2" > "$OUT_DIR/done-$1"
  echo "$2" > "$REVIEW_TMP/done-$1"
}
# publish_outputs: what the reviewers wrote, into $REVIEW_TMP, replacing whatever is there under
# those names, which no reviewer of this run wrote; 1 if any of it could not be, and PUBLISHED
# stays false: OUT_DIR, with the only copies, is then kept, and nothing is read from $REVIEW_TMP.
PUBLISHED=false PUBLISH_TRIED=false
publish_outputs() {
  local num name
  PUBLISH_TRIED=true
  for num in 1 2 3 4; do
    for name in "stdout-$num.txt" "stderr-$num.txt" "claude-raw-$num.json" "codex-out-$num.txt"; do
      # -r: a directory planted under the name made rm -f fail, and nothing was published.
      rm -rf -- "${REVIEW_TMP:?}/$name" || return 1
      # A regular file only: a symlink planted under the name would publish what it points to.
      if [ -f "$OUT_DIR/$name" ] && [ ! -L "$OUT_DIR/$name" ]; then
        cp -- "$OUT_DIR/$name" "$REVIEW_TMP/$name" || return 1
      fi
    done
  done
  PUBLISHED=true
}

# What the runner kills and removes, it keeps in its own memory, never in a file: a reviewer can
# write files in $REVIEW_TMP, and a planted pid-N of 1 would have the runner signal every process
# the user owns. JOB_PID holds each reviewer's job, set by its launcher, which runs in this shell,
# and JOB_BACKEND the CLI that decides how its output is read; the pid-N and backend-N.txt files
# are written only for watching from another shell. SCRATCH lists the scratch directories: agy's
# workspace and the copy of its sign-in token, which the agy job removes when agy exits, and
# gemini's workspace. A job killed by the watchdog or the sweep never gets that far, and neither
# does one whose runner is interrupted, so the runner removes them all: after the sweep, and on
# any exit. An interrupted runner
# also takes down the reviewers it started rather than leaving them running.
#
# kill_jobs: each job is one process group (`set -m`), and everything it starts stays in it:
# `timeout --foreground` does not move to a group of its own, as plain timeout does -- which left
# the reviewer under it running when the job's group was killed, and a helper the reviewer left
# behind unreachable once timeout had exited. bwrap's sandbox dies with bwrap (--die-with-parent).
# TERM first, then KILL for whatever ignored it.
JOB_PID=()
JOB_BACKEND=()   # which CLI ran each slot, which decides how its output is read (unwrap_envelope)
JOB_OUT=()       # the one file each slot's CLI writes its answer to, the only one read for it
JOB_PROVIDER=()  # whose model answers each slot, for the diversity count
JOB_MODEL=()     # the model launched for each slot, written into its result: hermes's as the
                 # loop labels it, claude's alias, CODEX_MODEL, agy's or gemini's; empty
                 # (codex on its default) keeps what the reviewer reported
SCRATCH=()
kill_jobs() {
  local sig
  for sig in TERM KILL; do
    [ "$sig" = KILL ] && sleep 1
    local num groups=() known=""
    [ "$GROUP_CHECK" = proc ] && known="$(session_groups)"
    for num in "$@"; do
      live_group "${JOB_PID[$num]:-}" "$known" && groups+=("-${JOB_PID[$num]}")
    done
    [ "${#groups[@]}" -eq 0 ] || kill -"$sig" -- "${groups[@]}" 2>/dev/null
  done
  return 0
}
# live_group PGID [GROUPS]: whether the group still has a member in this runner's session (GROUPS:
# session_groups, already read). bash reaps a finished job at once, and once its group has
# emptied, the number is free for the system to give
# to someone else's new session -- a shell, an ssh login -- which a kill of it would reach. A group
# of this run's always has its members in this run's session. Asked of pgrep where it can answer
# (GROUP_CHECK, decided below before anything is launched), else of /proc. A group number that
# merely exists is never enough: a reused one exists too.
live_group() {
  [ -n "$1" ] || return 1
  if [ "$GROUP_CHECK" = pgrep ]; then
    # $? said, not left implied: in a trap -- the cleanup runs in one -- a bare `return` gives
    # the status from before the trap (the `exit 143`), and every group read as gone.
    pgrep -s 0 -g "$1" >/dev/null 2>&1
    return $?
  fi
  case "${2:-$(session_groups)}" in *" $1 "*) return 0 ;; esac
  return 1
}
# session_groups: " PGID PGID ... ", every process group with a member in this runner's session,
# from one pass over /proc -- which kill_jobs makes once, not once per slot.
session_groups() {
  local stat line pgrp sid mine="" groups=" "
  for stat in "/proc/$$/stat" /proc/[0-9]*/stat; do
    # The whole file, to its end (-d ''): a command name can hold a newline, and the first line
    # alone then ends inside it. Empty if it went since the glob: not a member.
    line=""
    { IFS= read -r -d '' line < "$stat"; } 2>/dev/null
    [ -n "$line" ] || continue
    # After the command name, which can hold spaces and parentheses: state, ppid, pgrp, session.
    read -r _ _ pgrp sid _ <<< "${line##*) }"
    if [ -z "$mine" ]; then mine=$sid; continue; fi
    [ "$sid" = "$mine" ] && groups+="$pgrp "
  done
  echo "$groups"
}
# pgrep says 0 or 1; anything else -- not installed, no -s or -g -- is not an answer. Without
# either way to ask, a cleanup would have to guess, so the runner does not start.
if pgrep -s 0 -g 0 >/dev/null 2>&1 || [ $? -eq 1 ]; then
  GROUP_CHECK=pgrep
elif [ -r "/proc/$$/stat" ]; then
  GROUP_CHECK=proc
else
  echo "ERROR: the runner needs pgrep or /proc to tell its reviewers from other processes"; exit 1
fi
# Signals ignored first: a second Ctrl-C or TERM during the cleanup ran its trap's `exit`, which
# cut the cleanup short and left reviewers running.
# The reviewers' outputs are published here too if the run ends before the sweep did it, and OUT_DIR
# is removed only once they are: otherwise it holds the only copies, and is named.
finish_round() {
  trap '' INT TERM HUP QUIT
  kill_jobs 1 2 3 4
  remove_scratch
  rm -rf -- "$HELPER_ROOT"
  [ -n "${OUT_DIR:-}" ] || return 0
  # Once: a second try after a failed one would remove the copies the warning points to.
  $PUBLISH_TRIED || publish_outputs
  if $PUBLISHED; then
    rm -rf -- "$OUT_DIR"
  else
    echo "WARNING: the reviewers' outputs could not be copied into $REVIEW_TMP; kept in $OUT_DIR"
  fi
}
# remove_scratch: agy's scratch gone, or named: one left behind may hold a copy of its sign-in
# token, and an rm that failed said nothing. Made writable first, in case agy made it read-only.
remove_scratch() {
  [ "${#SCRATCH[@]}" -eq 0 ] && return 0
  local dir
  # A link where a directory was is removed as a link: chmod would change its target.
  for dir in "${SCRATCH[@]}"; do
    if [ -L "$dir" ]; then rm -f -- "$dir"; else chmod -R u+rwx -- "$dir" 2>/dev/null; fi
  done
  rm -rf -- "${SCRATCH[@]}"
  for dir in "${SCRATCH[@]}"; do
    [ -e "$dir" ] && echo "WARNING: could not remove $dir (agy's scratch); remove it by hand"
  done
  return 0
}
trap finish_round EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP   # a closed terminal or ssh session
trap 'exit 131' QUIT

# Per-reviewer deadline. The watchdog below allows a little more than this so that a reviewer
# killed by its own `timeout` is still collected as a failure rather than hanging the round.
# Each launcher below runs its CLI under `timeout` with `--foreground -k 10`: -k escalates to
# SIGKILL 10s after the TERM, and --foreground signals only the reviewer itself, not
# its helpers -- those are the sweep's, which kills each job's whole process group (kill_jobs).
REVIEWER_TIMEOUT=${REVIEWER_TIMEOUT:-600}
ROUND_DEADLINE=$((REVIEWER_TIMEOUT + 90))

# Every reviewer runs inside a brace group that records its exit status in a done-marker when it
# finishes. The markers are what makes progress observable from ANOTHER shell: `ps` on a recorded
# pid is ambiguous once pids are recycled, and an output file can exist while the process is still
# writing to it. The marker is written by the same subshell that ran the reviewer, NOT by a separate
# waiter - `wait` refuses a pid that is not a child of the shell calling it, so a sibling waiter
# would fail instantly and record a bogus status for a reviewer that is still running.

# Launch via Hermes. Its data stays in your AWS account, but -t file --yolo can still write files:
# its writes are not prevented, only seen afterwards. --ignore-rules: not the repository's
# AGENTS.md, SOUL.md or .cursorrules, which the branch under review chose, nor memory. Its JSON answer is read from stdout-N.txt.
# Hermes -z takes prompt as argument (no stdin mode). This means:
# - Subject to ARG_MAX for very large diffs (use Claude CLI instead)
# - Prompt content visible in /proc/PID/cmdline to local users
# For sensitive reviews with large diffs, prefer Claude CLI.
launch_hermes() {
  local num=$1 hermes_model=$2 prompt_file=$3 hermes_ws
  # From an empty directory outside the repository, as gemini is: hermes's -z path ignores
  # --ignore-rules (its one-shot agent is built without skip_context_files), so it reads the
  # branch's AGENTS.md and the rest from where it runs -- TERMINAL_CWD cleared -- and .hermes.md
  # up to a .git, which the workspace has, so the search ends there.
  make_scratch hermes_ws mmcra-ws- && mkdir -p "$hermes_ws/.git" \
    || { echo "R$num: SKIPPED (no directory for hermes)"; return 1; }
  case "$hermes_ws/" in
    "$(cd "$REVIEW_ROOT" && pwd -P)"/*)
      echo "R$num: SKIPPED (hermes's directory $hermes_ws is inside the repo; check TMPDIR)"
      return 1 ;;
  esac
  { cd "$hermes_ws" && env -u TERMINAL_CWD timeout --foreground -k 10 "$REVIEWER_TIMEOUT" \
      hermes -z "$(cat "$prompt_file")" \
      -m "$hermes_model" --provider bedrock -t file --yolo --ignore-rules \
      </dev/null \
      1>"$OUT_DIR/stdout-$num.txt" \
      2>"$OUT_DIR/stderr-$num.txt"
    mark_done "$num" $?; } &
  JOB_PID[$num]=$!
  JOB_OUT[$num]="$REVIEW_TMP/stdout-$num.txt"
  JOB_BACKEND[$num]=hermes
  echo hermes > "$REVIEW_TMP/backend-$num.txt"   # for watching
  JOB_PROVIDER[$num]=anthropic
  case "$hermes_model" in   # labelled as the convergence loop's prompts label them
    *opus*)   JOB_MODEL[$num]=claude-opus-4.6 ;;
    *sonnet*) JOB_MODEL[$num]=claude-sonnet-4.6 ;;
    *haiku*)  JOB_MODEL[$num]=claude-haiku-4.5 ;;
    *)        JOB_MODEL[$num]="$hermes_model" ;;
  esac
  echo $! > "$REVIEW_TMP/pid-$num"
  echo "R$num: Hermes ($hermes_model) [Bedrock]"
}

# prepare_agy N PROMPT_FILE: what agy's launch needs, in AGY_*, or 1 -- saying why -- so the slot goes
# to its next backend.
# - The result schema inline, not as a path: the sandbox hides home and /tmp, where the file lives.
#   Checked here, missing or not a schema: agy rejects a bad one only after launch, when the slot
#   can no longer go anywhere else.
# - An empty workspace outside the repository, and a directory for agy's state. Each is listed in
#   SCRATCH the moment it exists, and the runner removes them whatever happens next: no return
#   path has a cleanup of its own to forget. Resolved, because a relative TMPDIR names a different
#   place once the job has cd'd.
# - The wrapper in two parts, `env -i VAR=val...` and bwrap's, so that timeout runs between them:
#   after env has execed it, its command line -- which any local user can read -- holds none of the
#   values. The helper writes the first part's length, then both parts; just "0" from a host
#   trusted to be the sandbox itself (MMCRA_SKIP_AGY_SANDBOX_CHECK).
# - Its one stream-json input line, in a file (AGY_IN) as every other CLI's prompt is, and the
#   preamble into the prompt file itself, so the file is the record of what agy was sent. Last,
#   after every way this launch can still fall through to another backend, and in one step: the
#   prompt file is changed only once the input line is written.
AGY_SCHEMA="" AGY_WS="" AGY_STATE="" AGY_IN="" AGY_ENVP=() AGY_BWRAP=()
# make_scratch VAR [PREFIX]: a new temporary directory, named PREFIX (mmcra-tmp-) and random
# characters, listed in SCRATCH, resolved, into VAR.
make_scratch() {
  local dir
  dir="$(mktemp -d "${TMPDIR:-/tmp}/${2:-mmcra-tmp-}XXXXXXXX")" && [ -n "$dir" ] || return 1
  SCRATCH+=("$dir")
  dir="$(cd "$dir" && pwd -P)" || return 1
  printf -v "$1" %s "$dir"
}
prepare_agy() {
  local num=$1 prompt_file=$2 wrap=()
  AGY_SCHEMA="$(review_helper agy-schema "$HELPER_ROOT/docs/ensemble_review_result_schema.json")" \
    || { echo "R$num: SKIPPED (agy result schema unusable)"; return 1; }
  { make_scratch AGY_WS mmcra-ws- && make_scratch AGY_STATE mmcra-agy-state-; } \
    || { echo "R$num: SKIPPED (no temporary directory for agy)"; return 1; }
  case "$AGY_WS/" in
    "$(cd "$REVIEW_ROOT" && pwd -P)"/*)
      echo "R$num: SKIPPED (agy workspace $AGY_WS is inside the repo; check TMPDIR)"; return 1 ;;
  esac
  # The runner's private directories hidden too: outside /tmp, which is emptied inside, TMPDIR
  # would leave them readable to agy.
  mapfile -d '' -t wrap < <(review_helper agy-sandbox "$AGY_WS" "$AGY_STATE" "$REVIEW_ROOT" \
    "$OUT_DIR" "$PROMPT_DIR" "$HELPER_ROOT")
  [ "${#wrap[@]}" -ge 1 ] && [[ "${wrap[0]}" =~ ^[0-9]+$ ]] \
    || { echo "R$num: SKIPPED (could not build agy's sandbox)"; return 1; }
  AGY_ENVP=("${wrap[@]:1:${wrap[0]}}")
  AGY_BWRAP=("${wrap[@]:$((1 + wrap[0]))}")
  # Fail closed: the process substitution's status is not seen, so the wrapper itself is checked
  # -- env -i, then bwrap first and "--" last -- unless the host is trusted to be the sandbox.
  if ! review_helper flag MMCRA_SKIP_AGY_SANDBOX_CHECK; then
    [ "${#AGY_ENVP[@]}" -ge 2 ] && [[ "${AGY_ENVP[0]##*/}" == env ]] && [ "${AGY_ENVP[1]}" = -i ] \
      && [ "${#AGY_BWRAP[@]}" -ge 2 ] && [ "${AGY_BWRAP[-1]}" = -- ] \
      && [[ "${AGY_BWRAP[0]##*/}" == bwrap ]] \
      || { echo "R$num: SKIPPED (agy's sandbox wrapper is incomplete)"; return 1; }
  fi
  # The startup check described the host as it was then; what has appeared since -- a session
  # bus, a socket, an X server -- is checked again here, against the very wrapper agy will run
  # under, handed over on stdin.
  local why
  why="$(printf '%s\0' "${AGY_ENVP[@]}" "${AGY_BWRAP[@]}" | review_helper agy-reachable "$AGY_WS")" \
    || { echo "R$num: SKIPPED (agy not launched -- $why)"; return 1; }
  AGY_IN="$PROMPT_DIR/agy-in-$num.jsonl"
  review_helper agy-input "$prompt_file" "$AGY_IN" \
    || { echo "R$num: SKIPPED (could not prepare agy's prompt)"; return 1; }
  # The private copy now holds the preamble too; the record in $REVIEW_TMP is to say so as well.
  cp -- "$prompt_file" "$REVIEW_TMP/prompt-$num.txt" 2>/dev/null || true
}

# claude_deny N TOOLS: the --disallowedTools arguments for a claude reviewer allowed TOOLS, in
# CLAUDE_DENY, or 1 if they could not be worked out. --allowedTools only skips the permission
# prompt; it does not confine. So every tool that writes, runs a command or reaches the network,
# and that TOOLS does not name, is denied -- the convergence loop's list and its parsing
# (claude_tools.claude_denied), so the two cannot drift. A tool granted with a scope is granted whole
# as far as the deny list goes, which the helper says.
CLAUDE_DENY=() CLAUDE_TOOLS=""
claude_deny() {
  local num=$1 tools=$2 denied
  CLAUDE_DENY=()
  # And the only tools it has at all (--tools): a second layer, for a tool the deny list does not
  # name.
  CLAUDE_TOOLS="$(review_helper claude-tools "$tools")" || return 1
  denied="$(review_helper claude-deny "$tools" "$REVIEW_ROOT" 2>"$OUT_DIR/claude-deny-$num.err")" \
    || return 1
  sed "s/^/R$num: note: /" "$OUT_DIR/claude-deny-$num.err"
  [ -z "$denied" ] || CLAUDE_DENY=(--disallowedTools "$denied")
}

# Launch via a local reviewer CLI (stdin-based; each uses its own subscription or plan).
# Callers pass 'claude-<model>' (e.g. claude-opus, claude-sonnet,
# claude-haiku), 'codex', 'gemini', or 'agy'.
launch_cli() {
  local num=$1 cli=$2 prompt_file=$3

  case "$cli" in
    claude-*)
      local model="${cli#claude-}"
      if [[ ! "$model" =~ ^(opus|sonnet|haiku)$ ]]; then
        echo "R$num: SKIPPED (invalid Claude model: $model)"
        return 1
      fi
      # ALLOWED TOOLS ARE A REVIEW DECISION, not a detail. A read-only reviewer physically cannot
      # mutation-probe, and a reviewer asked to probe without the tools to do it may report a probe
      # result it never ran - that has happened. Set REVIEWER_TOOLS_2="Read Grep Glob Bash Edit
      # Write" for the correctness slot when you want real probing, and run that reviewer in an
      # isolated git worktree so its edits cannot reach the working tree.
      local tools_var="REVIEWER_TOOLS_$num"
      local tools="${!tools_var:-Read Grep Glob}"
      claude_deny "$num" "$tools" \
        || { echo "R$num: SKIPPED (could not work out what to deny the claude reviewer)"; return 1; }
      # --strict-mcp-config with no --mcp-config: none of the user's MCP servers, each a tool the
      # deny list does not name. --setting-sources user: not the project's settings, whose hooks,
      # apiKeyHelper and env -- chosen by the repository under review -- run commands no deny
      # list covers. --permission-mode dontAsk: only what is allowed, whatever mode the user's
      # settings choose (bypassPermissions would run anything).
      { timeout --foreground -k 10 "$REVIEWER_TIMEOUT" claude -p --model "$model" \
          --tools "$CLAUDE_TOOLS" --allowedTools "$tools" "${CLAUDE_DENY[@]}" \
          --strict-mcp-config --setting-sources user --permission-mode dontAsk \
          --output-format json < "$prompt_file" \
          1>"$OUT_DIR/claude-raw-$num.json" \
          2>"$OUT_DIR/stderr-$num.txt"
        mark_done "$num" $?; } &
      JOB_PID[$num]=$!
      JOB_OUT[$num]="$REVIEW_TMP/claude-raw-$num.json"
      JOB_BACKEND[$num]=claude
      echo claude > "$REVIEW_TMP/backend-$num.txt"   # for watching
      JOB_PROVIDER[$num]=anthropic
      # Named by its alias, not a version: opus, sonnet and haiku are aliases, and the version one
      # runs is claude's to say. Named all the same, since the prompt was written for the lens's
      # own model, and a slot that fell back to claude would otherwise report that one.
      JOB_MODEL[$num]="claude-$model"
      echo $! > "$REVIEW_TMP/pid-$num"
      echo "R$num: Claude CLI ($model, tools: $tools) [Claude subscription]"
      ;;
    codex)
      # Build extra flags from CODEX_* env vars.
      #   CODEX_MODEL=gpt-5.5            (or gpt-5.5-codex)
      #   CODEX_REASONING_EFFORT=high    -- "GPT-5.5 Pro" equivalent
      # Defaults: whatever ~/.codex/config.toml has (usually
      # model=gpt-5.5 + model_reasoning_effort=medium).
      # The safety flags are pinned, as in the convergence loop (_codex_extra_args): -s read-only;
      # approvals_reviewer="user", because a global "auto_review" escalates past read-only; and
      # project_doc_max_bytes=0, so that the branch's own AGENTS.md is not read as instructions.
      # And notify=[]: no command on its events, whatever a trusted project's .codex/config.toml
      # says. Its MCP servers are turned off one by one (codex-mcp-off): mcp_servers={} does not
      # (measured); not knowing which there are, it does not run, and the next backend does.
      local codex_extra=(-s read-only -c 'approvals_reviewer="user"' -c project_doc_max_bytes=0
        -c 'notify=[]') mcp_off
      mcp_off="$(review_helper codex-mcp-off "$REVIEW_ROOT")" \
        || { echo "R$num: SKIPPED (codex's MCP servers could not be listed)"; return 1; }
      [ -z "$mcp_off" ] || { mapfile -t mcp_off_args <<< "$mcp_off"; codex_extra+=("${mcp_off_args[@]}"); }
      [ -n "${CODEX_MODEL:-}" ] && codex_extra+=(-m "$CODEX_MODEL")
      [ -n "${CODEX_REASONING_EFFORT:-}" ] && \
        codex_extra+=(-c "model_reasoning_effort=$CODEX_REASONING_EFFORT")
      { timeout --foreground -k 10 "$REVIEWER_TIMEOUT" codex exec \
          "${codex_extra[@]}" \
          -o "$OUT_DIR/codex-out-$num.txt" \
          - < "$prompt_file" \
          1>"$OUT_DIR/stdout-$num.txt" \
          2>"$OUT_DIR/stderr-$num.txt"
        mark_done "$num" $?; } &
      JOB_PID[$num]=$!
      JOB_OUT[$num]="$REVIEW_TMP/codex-out-$num.txt"
      JOB_BACKEND[$num]=codex
      echo codex > "$REVIEW_TMP/backend-$num.txt"   # for watching
      JOB_PROVIDER[$num]=openai
      # Only a model named here overrides the result's: without one, codex runs its
      # own default, which the lens's label already names.
      JOB_MODEL[$num]="${CODEX_MODEL:-}"
      echo $! > "$REVIEW_TMP/pid-$num"
      local label="${CODEX_MODEL:-gpt-5.5}"
      [ -n "${CODEX_REASONING_EFFORT:-}" ] && label="$label/$CODEX_REASONING_EFFORT"
      echo "R$num: Codex CLI ($label) [ChatGPT subscription]"
      ;;
    gemini)
      # Gemini CLI: -p "appended to input on stdin". Omit -p flag
      # entirely and pipe stdin so the prompt IS the input.
      # Pin to gemini-2.5-flash because gemini-2.5-pro is not in the
      # free tier (returns HTTP 429). Override with GEMINI_MODEL=... if
      # you have paid quota.
      # In an empty directory of its own, not the repository under review, whose
      # .gemini/settings.json -- chosen by the change being reviewed -- it would load.
      # Its own, not inside OUT_DIR, where the other slots' outputs are.
      local gemini_ws
      make_scratch gemini_ws mmcra-ws- || { echo "R$num: SKIPPED (no directory for gemini)"; return 1; }
      case "$gemini_ws/" in
        "$(cd "$REVIEW_ROOT" && pwd -P)"/*)
          echo "R$num: SKIPPED (gemini's directory $gemini_ws is inside the repo; check TMPDIR)"
          return 1 ;;
      esac
      # Its upward searches end here: GEMINI.md up to a .git, the first environment file up the
      # tree -- the parent is a shared /tmp, where another user can put either.
      mkdir -p "$gemini_ws/.git" "$gemini_ws/.gemini" && : > "$gemini_ws/."env \
        && : > "$gemini_ws/.gemini/."env \
        || { echo "R$num: SKIPPED (could not prepare gemini's directory)"; return 1; }
      { cd "$gemini_ws" && timeout --foreground -k 10 "$REVIEWER_TIMEOUT" gemini \
          --model "${GEMINI_MODEL:-gemini-2.5-flash}" \
          --approval-mode plan --skip-trust \
          < "$prompt_file" \
          1>"$OUT_DIR/stdout-$num.txt" \
          2>"$OUT_DIR/stderr-$num.txt"
        mark_done "$num" $?; } &
      JOB_PID[$num]=$!
      JOB_OUT[$num]="$REVIEW_TMP/stdout-$num.txt"
      JOB_BACKEND[$num]=gemini
      echo gemini > "$REVIEW_TMP/backend-$num.txt"   # for watching
      JOB_PROVIDER[$num]=google
      JOB_MODEL[$num]="${GEMINI_MODEL:-gemini-2.5-flash}"
      echo $! > "$REVIEW_TMP/pid-$num"
      echo "R$num: Gemini CLI (${GEMINI_MODEL:-gemini-2.5-flash}) [Google AI Studio free tier]"
      ;;
    agy)
      # Antigravity CLI. Only reached when HAS_AGY=true, i.e. its sandbox check passed.
      #
      # agy itself confines nothing: measured, it wrote outside its workspace by absolute path
      # with no denial. So it runs under the bubblewrap wrapper from
      # scripts/review_loop/agy_sandbox.py (agy_wrapper_parts) -- built by calling it, so there is
      # one copy: filesystem read-only except an empty workspace OUTSIDE the repo, home hidden, a
      # throwaway copy of agy's state, no host processes or sockets, no capabilities. The prompt -- with the preamble telling it to call no tools
      # -- goes in as one stream-json line on stdin.
      prepare_agy "$num" "$prompt_file" || return 1
      JOB_BACKEND[$num]=agy
      echo agy > "$REVIEW_TMP/backend-$num.txt"
      # Every descriptor the runner holds beyond 0-2 closed first: bwrap would hand each into
      # the sandbox, where the check -- which runs from Python, with them closed -- never saw it.
      # Listed before the loop: its own 2>/dev/null saves stderr in a descriptor of its own,
      # which a list taken inside it would include, and closing it lost the job's stderr.
      { fds=(/proc/$BASHPID/fd/*)
        for fd in "${fds[@]}"; do
          fd=${fd##*/}; [[ $fd =~ ^[0-9]+$ ]] && [ "$fd" -gt 2 ] && eval "exec $fd>&-"
        done 2>/dev/null
        cd "$AGY_WS" && "${AGY_ENVP[@]}" timeout --foreground -k 10 "$REVIEWER_TIMEOUT" "${AGY_BWRAP[@]}" agy \
          --sandbox --disable-slash-commands \
          --model "$AGY_SELECTED_MODEL" \
          --input-format stream-json --output-format stream-json \
          --print-timeout 0 --json-schema "$AGY_SCHEMA" \
          < "$AGY_IN" \
          1>"$OUT_DIR/stdout-$num.txt" \
          2>"$OUT_DIR/stderr-$num.txt"
        # agy's status held while its scratch -- with the copy of its sign-in token -- is
        # removed, and the done marker written last: by the time the watchdog sees this slot
        # finished, the copy is gone.
        rc=$?; rm -rf "$AGY_WS" "$AGY_STATE"; mark_done "$num" "$rc"; } &
      JOB_PID[$num]=$!
      JOB_OUT[$num]="$REVIEW_TMP/stdout-$num.txt"
      # The family of the model agy runs, which AGY_MODEL can make Claude's or GPT's.
      case "$AGY_SELECTED_MODEL" in
        claude-*) JOB_PROVIDER[$num]=anthropic ;;
        gpt-*)    JOB_PROVIDER[$num]=openai ;;
        *)        JOB_PROVIDER[$num]=google ;;
      esac
      JOB_MODEL[$num]="$AGY_SELECTED_MODEL"
      echo $! > "$REVIEW_TMP/pid-$num"
      echo "R$num: Antigravity CLI ($AGY_SELECTED_MODEL) [Google AI plan]"
      echo "R$num: agy sends diff, audit and context to Google (REVIEW_USE_AGY=0 to stop)"
      ;;
    *)
      echo "R$num: SKIPPED (unknown backend: $cli)"
      return 1
      ;;
  esac
}

# DETECTION, here and not by the caller: each slot goes to the first backend in the routing tables
# (.claude/agents/ensemble-review.md, "CLI and provider detection") that is installed and passes
# the convergence loop's own checks.
HAS_HERMES=false; command -v hermes &>/dev/null && HAS_HERMES=true
HAS_CLAUDE=false; command -v claude &>/dev/null && HAS_CLAUDE=true
HAS_CODEX=false;  command -v codex  &>/dev/null && HAS_CODEX=true
HAS_GEMINI=false; command -v gemini &>/dev/null && HAS_GEMINI=true
# Bedrock: when the user asked for it (--hermes), or the project pinned it with .use-hermes -- in
# the checkout, or at any base the diff may be taken against: origin/main, the base full mode
# diffs against, as the convergence loop reads it from --base. A branch that deletes the pin would
# otherwise send its own code to OpenAI and Google.
PREFER_HERMES=$HERMES_ASKED
[ -f .use-hermes ] && PREFER_HERMES=true
for ref in origin/main "$(git merge-base origin/main HEAD 2>/dev/null)" HEAD~1 HEAD; do
  [ -n "$ref" ] && git cat-file -e "$ref:.use-hermes" 2>/dev/null && PREFER_HERMES=true
done
# codex: only once its sandbox is shown to refuse a write (no model call). MMCRA_SKIP_CODEX_SANDBOX_CHECK=1
# (or true/yes/on -- not 0) skips the check on a host that is already a sandbox.
if $HAS_CODEX; then review_helper codex-check || HAS_CODEX=false; fi
# agy (Antigravity CLI): on wherever it is installed; REVIEW_USE_AGY set to anything but
# 1/true/yes/on switches it off. The model is AGY_MODEL if set, else the best Gemini model
# `agy models` lists for this sign-in (newest version, then Pro/Argon over Flash over Lite, then
# medium effort over high over low) -- picked fresh each run, so a newer model is used once the
# plan has it. Only true once the sandbox check has passed (no model call). A project pinned to
# Bedrock does not run agy at all -- not even this check -- unless REVIEW_USE_AGY=1 and Hermes is
# not installed (Hermes answers slot 3 when it is). Status lines go to stderr; the model it settled
# on is the only thing on stdout. Its own variable, not AGY_MODEL: written back there, the pick
# would be taken next time for a model asked for by name.
HAS_AGY=false AGY_SELECTED_MODEL=""
if command -v agy &>/dev/null; then
  AGY_SELECTED_MODEL="$(review_helper agy-detect "$PREFER_HERMES")"
  [ -n "$AGY_SELECTED_MODEL" ] && HAS_AGY=true
fi
echo "Backends: claude=$HAS_CLAUDE codex=$HAS_CODEX agy=$HAS_AGY gemini=$HAS_GEMINI" \
  "hermes=$HAS_HERMES; Bedrock preferred: $PREFER_HERMES"

# Pre-check
if ! $HAS_CLAUDE && ! $HAS_HERMES; then
  echo "ERROR: neither claude nor hermes installed. Use quick mode."
  exit 1
fi

# The prompts each launcher reads, copied first into a directory of the runner's own: in
# $REVIEW_TMP, a reviewer already running with a file tool (hermes -t file --yolo) could rewrite a
# later slot's before it was read. The copies there stay as the record.
make_scratch PROMPT_DIR mmcra-prompt- \
  && cp -- "$REVIEW_TMP"/prompt-[1-4].txt "$PROMPT_DIR/" \
  || { echo "ERROR: could not copy the reviewers' prompts"; exit 1; }

# R1: Security (Opus) -- local CLI by default (free), Hermes only when opted in
if $PREFER_HERMES && $HAS_HERMES && launch_hermes 1 us.anthropic.claude-opus-4-6-v1 "$PROMPT_DIR/prompt-1.txt"; then
  :  # launched; if hermes could not be set up, the next backend runs
elif $HAS_CLAUDE && launch_cli 1 claude-opus "$PROMPT_DIR/prompt-1.txt"; then
  :  # launched; if claude could not be set up, Hermes is next, as the routing tables say
elif $HAS_HERMES; then
  launch_hermes 1 us.anthropic.claude-opus-4-6-v1 "$PROMPT_DIR/prompt-1.txt"
fi

# R2: Correctness -- Codex (GPT-5.5) preferred for cross-provider diversity.
# When PREFER_HERMES is set, the Bedrock haiku path runs first.
if $PREFER_HERMES && $HAS_HERMES && launch_hermes 2 us.anthropic.claude-haiku-4-5-20251001-v1:0 "$PROMPT_DIR/prompt-2.txt"; then
  :  # launched; if hermes could not be set up, the next backend runs
elif $HAS_CODEX && { ! $PREFER_HERMES || review_helper flag REVIEW_USE_CODEX; } \
     && launch_cli 2 codex "$PROMPT_DIR/prompt-2.txt"; then
  # A project pinned to Bedrock does not fall to OpenAI by default when Hermes is unavailable.
  # If codex could not be set up (its MCP servers unlisted), the next backend runs.
  :
elif $HAS_CLAUDE && launch_cli 2 claude-haiku "$PROMPT_DIR/prompt-2.txt"; then
  :  # launched; if claude could not be set up, Hermes is next
elif $HAS_HERMES; then
  launch_hermes 2 us.anthropic.claude-haiku-4-5-20251001-v1:0 "$PROMPT_DIR/prompt-2.txt"
fi

# R3: Readability -- Google preferred (3rd provider for diversity): agy when installed and its
# sandbox verified, else Gemini only if asked for (REVIEW_USE_GEMINI=1), as in the convergence
# loop: the gemini CLI explores the repository with its own tools and has not produced a finding
# (README). When PREFER_HERMES is set, the Bedrock sonnet path runs first, and agy too only if
# asked for by name (REVIEW_USE_AGY=1): a project that pinned Bedrock to keep its code with
# providers it chose must not fall to Google by default when Hermes is unavailable.
# `${HAS_AGY:-false}`, not `$HAS_AGY`: an unset variable expands to an empty command, which
# succeeds, and would route the slot to a backend nobody verified.
if $PREFER_HERMES && $HAS_HERMES && launch_hermes 3 us.anthropic.claude-sonnet-4-6 "$PROMPT_DIR/prompt-3.txt"; then
  :  # launched; if hermes could not be set up, the next backend runs
elif ${HAS_AGY:-false} && [ -n "${AGY_SELECTED_MODEL:-}" ] \
    && { ! $PREFER_HERMES || review_helper flag REVIEW_USE_AGY; } \
     && launch_cli 3 agy "$PROMPT_DIR/prompt-3.txt"; then
  :  # launched; if agy could not be set up, launch_cli returned 1 and the next backend runs
elif $HAS_GEMINI && review_helper flag REVIEW_USE_GEMINI \
     && launch_cli 3 gemini "$PROMPT_DIR/prompt-3.txt"; then
  :  # launched; if gemini could not be set up, the next backend runs, as for agy
elif $HAS_CLAUDE && launch_cli 3 claude-sonnet "$PROMPT_DIR/prompt-3.txt"; then
  :  # launched; if claude could not be set up, Hermes is next
elif $HAS_HERMES; then
  launch_hermes 3 us.anthropic.claude-sonnet-4-6 "$PROMPT_DIR/prompt-3.txt"
fi

# R4: Spec-contract (Opus) -- local CLI by default (free), Hermes only when opted in
if $PREFER_HERMES && $HAS_HERMES && launch_hermes 4 us.anthropic.claude-opus-4-6-v1 "$PROMPT_DIR/prompt-4.txt"; then
  :  # launched; if hermes could not be set up, the next backend runs
elif $HAS_CLAUDE && launch_cli 4 claude-opus "$PROMPT_DIR/prompt-4.txt"; then
  :  # launched; if claude could not be set up, Hermes is next, as the routing tables say
elif $HAS_HERMES; then
  launch_hermes 4 us.anthropic.claude-opus-4-6-v1 "$PROMPT_DIR/prompt-4.txt"
fi

# Provider diversity, counted from what was launched: predicted from what is installed, it counted
# codex and Google for a project pinned to Bedrock, where Hermes takes slots 2 and 3.
N_PROVIDERS=$(printf '%s\n' "${JOB_PROVIDER[@]}" | sort -u | grep -c .)
if [ "$N_PROVIDERS" -eq 0 ]; then
  echo "WARNING: no reviewer was launched."
elif [ "$N_PROVIDERS" -eq 1 ]; then
  echo "WARNING: single-provider mode ($(printf '%s\n' "${JOB_PROVIDER[@]}" | sort -u | grep .) only)."
  echo "Install codex and/or agy (or gemini, with REVIEW_USE_GEMINI=1) for cross-provider diversity."
fi
echo "Provider diversity: $N_PROVIDERS provider(s)"

# WATCHDOG, not a bare `wait`. A bare wait has no deadline of its own, so one wedged reviewer holds
# the whole round open forever and the partial results from the other three are never reported.
# This gives up at ROUND_DEADLINE and leaves the stragglers to be recorded as timed out below.
round_start=$SECONDS
while :; do
  running=0
  for num in 1 2 3 4; do
    [ -n "${JOB_PID[$num]:-}" ] || continue            # slot was skipped, never launched
    [ -f "$OUT_DIR/done-$num" ] && continue            # finished, status recorded
    running=$((running + 1))
  done
  [ "$running" -eq 0 ] && break
  if [ $((SECONDS - round_start)) -ge "$ROUND_DEADLINE" ]; then
    echo "WATCHDOG: $running reviewer(s) still running after ${ROUND_DEADLINE}s - giving up on them"
    for num in 1 2 3 4; do
      [ -n "${JOB_PID[$num]:-}" ] || continue
      [ -f "$OUT_DIR/done-$num" ] && continue
      kill_jobs "$num"
      mark_done "$num" timeout
    done
    break
  fi
  # In the background and waited for: bash runs a trap only once the foreground command ends, so
  # a plain `sleep 5` held off an interrupt's cleanup for up to five seconds; `wait` returns at once.
  sleep 5 & wait $!
done
echo "All reviewer slots resolved after $((SECONDS - round_start))s"

# SWEEP. A reviewer killed by its own `timeout`, or one that exited, can still leave helpers:
# timeout signals the process it started, not that process's children. Every job is its own process
# group, helpers included, so one group kill per slot collects them. Harmless when it is gone.
kill_jobs 1 2 3 4
# Now, not at exit: a killed agy job never reached its own cleanup of the token copy.
remove_scratch
# Every reviewer is stopped: nothing is left to write what is copied.
publish_outputs || echo "WARNING: could not copy the reviewers' outputs into $REVIEW_TMP"

# Extract review JSON from raw output, as the convergence loop does: Claude's wrapper, fences and
# preamble; agy's stream only when agy ran the slot (any reviewer can be talked into printing a line
# shaped like agy's), and only a turn that finished. Then the loop's own validation: the required
# keys, findings that are a list of objects, and the reviewer named by the slot, not by the model.
extract_json() {
  review_helper extract "$1" "$2" "$3" "${4:-}" "${5:-}" \
    2>"$REVIEW_TMP/extract-$(basename "$2" .json).err"
}

# Post-process: normalize all output to result-N.json.
for num in 1 2 3 4; do
  result="$REVIEW_TMP/result-$num.json"
  # No CLI writes this file itself, so one already here was planted; only extraction writes it.
  # One that cannot be removed would pass for this round's review: stop rather than leave it.
  # -r: a directory planted under the name made rm -f fail, and the run ended with no results.
  rm -rf -- "$result" || { echo "ERROR: cannot remove $result, which would pass for a review"; exit 1; }

  # Only the file the slot's own CLI writes, as recorded at launch: any other file there, named
  # like another CLI's output, may have been put there by a reviewer with a file tool. Checked as
  # the loop checks it, codex's answer included.
  src="${JOB_OUT[$num]:-}"
  $PUBLISHED && [ -n "$src" ] && [ -f "$src" ] \
    && extract_json "$src" "$result" "$num" "${JOB_BACKEND[$num]:-}" "${JOB_MODEL[$num]:-}"

  # If no result produced, write a synthetic error. The done-marker distinguishes the two very
  # different failures a reader needs to tell apart: a reviewer that ran and produced unparseable
  # output, and one that never finished at all.
  if [ ! -f "$result" ]; then
    case "$(cat "$OUT_DIR/done-$num" 2>/dev/null)" in
      timeout) why="killed by the watchdog before it finished" ;;
      124|137) why="hit its own ${REVIEWER_TIMEOUT}s timeout" ;;
      "")      why="no parseable JSON output and no exit status recorded" ;;
      0)       why="no parseable JSON output (it exited cleanly, so read its stdout)" ;;
      *)       why="exited $(cat "$OUT_DIR/done-$num") without usable JSON" ;;
    esac
    [ -n "${JOB_PID[$num]:-}" ] || why="never launched (no backend available for this slot)"
    $PUBLISHED || why="its output could not be copied into \$REVIEW_TMP (kept in $OUT_DIR)"
    review_helper placeholder "$result" "$num" "$why"
    echo "WARNING: Reviewer $num produced no result ($why)"
  fi
done

echo "ROUND COMPLETE"
exit 0
}
