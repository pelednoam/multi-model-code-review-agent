# Multi-Model Code Review Agent

![Multi-Model Code Review Agent](docs/img/multi-model-review-agent.png)

A code review pipeline that runs multiple frontier LLMs in parallel,
each with a different review lens, preceded by a deterministic
preflight audit. Three modes:

- **Quick mode** (free): 2 Claude Code subagents with clean context.
  ~30 seconds. Use for every commit.
- **Full mode** (free or paid): 4 parallel reviewers using native CLIs
  (Claude Code, Codex, Gemini) or Hermes+Bedrock. ~3-5 minutes. Use
  for pre-merge gates.
- **Convergence loop** (free or paid): runs full mode repeatedly,
  applying fixes via a clean-context merge agent, until no blocking
  findings remain (or the same findings appear twice -- "stuck"). Each
  round enforces a mandatory four-step CI gate before commit.

All three modes run the same preflight audit and spec-contract review
lens. Designed for codebases where static diff review alone misses
contract violations between code and signed artifacts.

## Why not just ask Claude Code to review?

When you ask Claude Code to review its own work -- or spawn a subagent
to do it -- you get a fast, convenient review. But it has structural
limitations that matter for high-stakes code:

**Same model, same blind spots.** Claude Code and its subagents all run
on the same model family. Every model has systematic blind spots shaped
by its training data and RLHF tuning. Running Opus, GPT-5.5, and
Gemini on the same diff surfaces findings that no single model catches
alone. In our testing, the spec-contract reviewer (Opus) caught manifest
drift that the correctness reviewer (GPT-5.5) missed, while GPT-5.5
caught dtype coercion bugs that Opus didn't flag.

**Contaminated context.** When the dev agent reviews its own output, it
has seen the implementation rationale, the rejected alternatives, and
the conversation leading to each decision. A reviewer with a clean
context -- seeing only the diff, the spec, and the audit output --
evaluates the code as a future reader would, not as the author.

**No separation of concerns.** The dev agent has write access, tool
access, and conversational momentum. A review-only agent with read-only
file access and no terminal cannot accidentally fix what it finds,
cannot be influenced by prior conversation, and cannot be prompted into
leniency by the author's explanations.

**No artifact awareness.** A code review in the same session sees the
diff but not the repo's signed manifests, artifact SHAs, or runtime
state. This pipeline runs a deterministic preflight audit that
machine-verifies manifest counts, feature dimensions, and artifact
integrity _before_ any LLM runs. In our testing, this caught a
782-vs-662 feature count mismatch that three rounds of same-session
review missed.

### What this pipeline adds

| Capability | Same-session | Quick mode | Full: CLIs | Full: Hermes |
|---|---|---|---|---|
| Clean context | no | yes | yes | yes |
| Deterministic preflight | no | yes | yes | yes |
| Artifact/manifest audit | no | yes | yes | yes |
| Coverage-gap detection | no | yes | yes | yes |
| Per-reviewer attribution | no | 2 lenses | 4 lenses | 4 lenses |
| Write isolation | no | no | `--allowedTools` | `-t file` |
| Structured JSON schema | no | no | yes | yes |
| Review packet persistence | no | no | yes | yes |
| Cross-provider models | no | no | with Codex+Gemini | any provider |
| Mandatory CI gate before commit | no | no | yes | yes |
| Cost | free | free | free | ~$5-40 |
| Latency | instant | ~30s | ~3 min | ~5 min |

**When to use which:**
- **Quick mode**: every commit, rapid iteration (free, 30s)
- **Full mode with CLIs**: pre-merge gates when cost matters (free, 3min)
- **Full mode with Hermes**: high-stakes changes where Bedrock's
  data-at-rest guarantees matter

## Architecture

Each reviewer runs as a **separate OS process** with clean context --
no conversation history, no author bias.

```
Claude Code (or shell)
  |
  |  "review this" (quick), "full review" (full),
  |  or "run until converged" (loop)
  v
ensemble-review agent (docs/ensemble-review.md)
  |
  +-------- [convergence loop, repeats until converged or stuck] -------+
  |                                                                    |
  |  1. git diff | scrub_diff.py  (secrets never touch disk)            |
  |  2. review_preflight.py       (audit + coverage gaps)               |
  |  3. build context bundle      (docs, manifests, specs)              |
  |                                                                    |
  |  QUICK MODE:                         FULL MODE:                    |
  |  Agent(opus, spec-contract)          R1: hermes opus  OR  claude -p |
  |  Agent(sonnet, correctness)          R2: codex exec   (GPT-5.5)    |
  |  (2 subagents, free)                 R3: gemini       OR  claude -p |
  |                                      R4: hermes opus  OR  claude -p |
  |                                      (4 sessions, free or paid)    |
  |                                                                    |
  |  4. synthesize convergence report                                  |
  |  5. [loop only] merge agent applies suggested_fix's (clean ctx)    |
  |  6. [loop only] mandatory gate: ruff + format + mypy + pytest      |
  |  7. [loop only] auto-commit; fingerprint findings vs prev round    |
  +--------------------------------------------------------------------+
  v
User sees: preflight audit, blocking findings, convergence analysis
```

### Full mode routing

The agent auto-detects installed CLIs and picks the best backend for
each reviewer. **Native CLIs are preferred** because each CLI manages
its own auth reliably (no stale OAuth tokens), and they're free.
Hermes is preferred for Anthropic models only when Bedrock isolation
matters.

| Reviewer | 1st choice | 2nd choice | 3rd choice |
|---|---|---|---|
| Security (Opus) | Hermes Opus (Bedrock) | Claude CLI opus (free) | -- |
| Correctness | Codex CLI GPT-5.5 (free) | Hermes Haiku (Bedrock) | Claude CLI haiku (free) |
| Readability | Gemini CLI (free) | Hermes Sonnet (Bedrock) | Claude CLI sonnet (free) |
| Spec-contract (Opus) | Hermes Opus (Bedrock) | Claude CLI opus (free) | -- |

**Why native CLIs over Hermes API proxying**: each CLI manages its own
auth session. Hermes's OAuth proxying (e.g. Codex OAuth) can have
stale tokens and intermittent failures. `codex exec` uses your ChatGPT
subscription directly -- same as opening a new terminal tab.

**With all CLIs installed** (claude + codex + gemini): the full review
is free, 3 provider families (Anthropic + OpenAI + Google).

## Coverage-gap detection

The preflight audit runs `pytest --cov-branch` against changed source
files (anything under `src/` or `research/` in the diff) and emits a
per-file `coverage_gaps` list with the actual uncovered line numbers
and branch arcs. The correctness reviewer is instructed to turn each
gap into a concrete test-case finding with `suggested_fix` containing
the runnable test code -- not a description.

The merge agent applies those test cases verbatim, so a single
convergence round closes the loop: detect uncovered code -> write the
test -> verify coverage rises -> pass the gate -> commit.

Coverage target is configurable (default 100%) via `COVERAGE_TARGET`
at the top of `scripts/review_preflight.py`. Set it to your project's
policy. Per-file opt-out is the standard `# pragma: no cover`.

## The convergence loop (external loop)

`scripts/review_until_converged.py` is an **external orchestration
loop** that wraps the inner review pipeline. Where quick and full
modes do one review pass, the convergence loop runs the full pipeline
**autonomously, round after round**, applying fixes between rounds
until no blocking findings remain. You start it once; it stops on its
own.

### What one round looks like

```
+------------------------------------------------------------------+
|  ROUND N                                                         |
|                                                                  |
|  1. preflight audit  (git state + manifests + coverage gaps)     |
|  2. 4 reviewers run IN PARALLEL on the current diff              |
|     (each in a separate OS process, clean context)               |
|  3. fingerprint findings -> compare with previous round          |
|  4. merge agent applies suggested_fix's IN A CLEAN CONTEXT       |
|     (separate Claude Code session; sees only the findings)       |
|  5. mandatory gate: ruff check + ruff format + mypy + pytest     |
|  6. auto-commit if --auto-commit                                 |
+------------------------------------------------------------------+
```

Each step writes evidence under `data/reviews/<timestamp>_<branch>/round-N/`:

- `audit.json` -- preflight output
- `prompt-{1..4}.txt` + `result-{1..4}.json` -- reviewer prompts and parsed findings
- `merge-prompt.txt` + `merge-output.json` -- what the merge agent saw and did
- `gate-output.txt` -- combined output of all four gate steps
- `convergence.json` -- diff vs previous round (new / repeated / resolved findings)

### Stop conditions

The loop exits on the first of:

| Code | Reason | When |
|---|---|---|
| 0 | **Converged** | No blocking findings remain -- only suggestions |
| 2 | **Stuck** | The same fingerprinted blocking findings appeared two rounds in a row (the reviewers want a fix the merge agent can't or won't make) |
| 3 | Merge agent failed | The merge subprocess crashed or returned non-JSON |
| 4 | Gate failed | Lint / format / mypy / pytest failed after fixes were applied |
| 5 | Commit/push failed | `--auto-commit` was set and git refused the push |
| 6 | Max rounds reached | `--max-rounds` (default 5) hit without convergence |

Exit 4 means the loop stops **without rolling back** -- the failing
diff is left in the working tree so you can inspect what went wrong.
Exit 2 is the most useful diagnostic: when two rounds produce
identical blocking findings, that's a signal that human judgment is
needed (the reviewers disagree with each other, or the fix requires
architectural changes the merge agent can't make).

### Why the merge agent runs in a clean context

The orchestrator (the convergence loop itself) has seen everything:
the prior rounds, the diff, the reviewer prompts, the failed gate
output. That context is contaminating -- if the orchestrator applied
fixes, it could be influenced by the rationale of an earlier rejected
fix or by the author's coding style from elsewhere in the diff.

The merge agent runs as a **separate Claude Code session** that sees
only:
- the current diff
- the parsed findings (file, issue, suggested_fix)
- the repo layout for navigation

It cannot see prior conversation, prior rounds, or the orchestrator's
notes. Findings either get applied verbatim or not at all -- there's
no negotiation in the loop.

### Invoking the loop

From Claude Code: "run until converged".

From the shell:

```bash
python scripts/review_until_converged.py \
    --repo /path/to/repo \
    --max-rounds 5 \
    --auto-commit            # commit after each successful round
```

Common patterns:

| Goal | Flags |
|---|---|
| One-shot: review + fix + gate, then stop | `--max-rounds 1` |
| Iterate locally without polluting git | omit `--auto-commit`, inspect each round's working tree |
| CI gate enforcement only | use the loop with `--max-rounds 1`; the gate runs unconditionally |
| Long autonomous session | `--max-rounds 10 --auto-commit` |

### When to use which mode

- **Quick mode**: every commit -- catch obvious problems, ~30s
- **Full mode**: pre-merge gate -- cross-provider diversity, ~3 min
- **Convergence loop**: when you want the agent to *finish* the
  review-and-fix cycle, not just identify problems. Good for: large
  refactors, post-rebase cleanup, "make this PR mergeable", catching
  regressions after a bulk auto-edit.

## Mandatory CI gate

Step 5 of each convergence-loop round is a **four-step gate** that
must pass before commit:

1. `ruff check .`
2. `ruff format --check .`
3. `mypy scripts/` (or the project's target)
4. `pytest tests/ -x -q`

If any step fails, the loop stops without committing -- the round's
`gate-output.txt` captures which step failed and why. Missing tools
(e.g. mypy not installed in the venv) cause a graceful skip with a
note rather than a failure, so the gate works in minimal environments.

The gate closes the lint/type/test loop: no review can recommend a
fix that breaks the gate without the loop catching it on the same
round. If a review's `suggested_fix` is applied and breaks tests, the
loop halts at the broken commit so you can inspect, instead of
silently moving on.

## Why four lenses?

| Reviewer | Focus |
|---|---|
| **Security & robustness** | Injection, auth, deserialization, secrets, race conditions |
| **Correctness & edge cases** | Off-by-one, shape mismatches, contract violations, logic errors |
| **Readability & performance** | Naming, dead code, N+1 I/O, memory growth, vectorization |
| **Spec-contract compliance** | Code vs signed artifacts, manifest drift, feature count mismatches, fallback logic that silently changes cohorts |

The spec-contract reviewer is the key differentiator. It catches the
class of failures that pure code review misses: a code path using 782
features when the signed manifest says 662, a target mask rebuilt from
labels instead of using the pinned `.npy`, a test asserting current
behavior instead of specified behavior.

## Prerequisites

**Quick mode** (free): just Claude Code. No additional setup.

**Full mode**: at least `claude` or `hermes` must be installed for
Anthropic reviewers. For cross-provider diversity, also install:
- [Codex CLI](https://github.com/openai/codex) for GPT-5.5
  (uses ChatGPT subscription, no API credits)
- [Gemini CLI](https://www.npmjs.com/package/@google/gemini-cli) for
  Gemini (uses Google AI Studio, free tier available)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) +
  AWS Bedrock for best isolation (data stays in your AWS account)

## Quick start

### 1. Copy the agent definition to your project

```bash
mkdir -p .claude/agents
cp docs/ensemble-review.md .claude/agents/ensemble-review.md
```

### 2. Copy the scripts

```bash
cp scripts/scrub_diff.py             your-project/scripts/
cp scripts/review_preflight.py       your-project/scripts/
cp scripts/review_until_converged.py your-project/scripts/   # optional, for the loop
cp scripts/validate_review_results.py your-project/scripts/
cp docs/ensemble_review_result_schema.json your-project/docs/
```

### 3. Configure for your project

Edit `scripts/review_preflight.py` to set:
- `SIGNED_MANIFESTS`: paths to your project's signed JSON artifacts
- `ARTIFACT_DIRS`: directories containing JSON artifacts to audit

### 4. Run from Claude Code

Say any of:
- "review this" or "quick review" -- quick mode (free, 2 subagents)
- "full review" or "full ensemble review" -- full mode (4 reviewers)
- "run until converged" -- convergence loop, see [The convergence
  loop](#the-convergence-loop-external-loop) for details

## Files

| File | Purpose |
|---|---|
| `docs/ensemble-review.md` | Claude Code agent definition |
| `docs/ENSEMBLE_REVIEW.md` | Detailed usage guide |
| `docs/ensemble_review_result_schema.json` | Strict JSON schema for reviewer output |
| `scripts/scrub_diff.py` | Stdin credential scrubber |
| `scripts/review_preflight.py` | Deterministic pre-review audit (now includes coverage-gap detection) |
| `scripts/review_until_converged.py` | Convergence loop: review -> merge -> mandatory gate -> commit, until convergence |
| `scripts/validate_review_results.py` | JSON schema validator |
| `tests/test_review_scripts.py` | Test suite |

## Key design decisions

- **Native CLIs preferred over API proxying**: `codex exec` manages
  its own ChatGPT auth reliably; `claude -p` manages Anthropic auth;
  `gemini` manages Google auth. Hermes API proxying (e.g. Codex OAuth)
  has intermittent token expiry issues. Use native CLIs by default,
  Hermes only when you need Bedrock isolation.
- **Claude Code orchestrates**: Hermes `delegate_task` cannot route
  children to different models. Claude Code spawns parallel processes.
- **Secrets never touch disk**: `scrub_diff.py` reads stdin via pipe.
  Exits non-zero on redaction, blocking the review until secrets are
  removed and rotated.
- **Evidence-based convergence**: a single finding with artifact
  evidence is high-priority even if only one reviewer flags it.
- **Strict schema**: `additionalProperties: false` at both levels.
  Extracted JSON is validated for required keys before writing.
- **Private temp directory**: `mktemp -d`, not world-readable `/tmp/`.

## Privacy and isolation

| Backend | Data path | Isolation | Cost |
|---|---|---|---|
| Hermes+Bedrock | Your AWS account | Best (`-t file`) | Bedrock pricing |
| Claude CLI | Anthropic infrastructure | Good (`--allowedTools`) | Claude subscription |
| Codex CLI | OpenAI infrastructure | Good (sandbox) | ChatGPT subscription |
| Gemini CLI | Google infrastructure | Good (`--approval-mode plan`) | AI Studio free tier |

For sensitive/FDA-path code where data-at-rest guarantees matter, use
Hermes+Bedrock for all reviewers.

## Customization

### Add/remove review lenses

Edit the agent definition. The JSON schema accepts any string for the
`reviewer` field -- no enum constraint.

### Add credential patterns

Edit `CREDENTIAL_PATTERNS` in `scripts/scrub_diff.py`. Current
coverage: API keys, passwords, tokens, private keys, AWS keys, OpenAI
keys, GitHub PATs, GitLab tokens, GCP service accounts, Azure
connection strings.

## License

MIT
