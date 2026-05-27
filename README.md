# Multi-Model Code Review Agent

A two-tier code review pipeline with a deterministic preflight audit:

- **Quick mode** (free): 2 Claude Code subagents with clean context.
  ~30 seconds. Use for every commit.
- **Full mode** (paid): 4 parallel
  [Hermes Agent](https://github.com/NousResearch/hermes-agent) sessions
  across providers (Opus, GPT-5.5, Sonnet). ~5 minutes. Use for
  pre-merge gates.

Both modes run the same preflight audit and spec-contract review lens.
Designed for codebases where static diff review alone misses contract
violations between code and signed artifacts.

## Why not just ask Claude Code to review?

When you ask Claude Code to review its own work -- or spawn a subagent
to do it -- you get a fast, convenient review. But it has structural
limitations that matter for high-stakes code:

**Same model, same blind spots.** Claude Code and its subagents all run
on the same model family. Every model has systematic blind spots shaped
by its training data, architecture, and RLHF tuning. Running the same
model twice doesn't find what it can't see. Running Opus, GPT-5.5, and
Sonnet on the same diff surfaces findings that no single model catches
alone. In our testing, the spec-contract reviewer (Opus) caught manifest
drift that the correctness reviewer (GPT-5.5) missed, while GPT-5.5
caught dtype coercion bugs that Opus didn't flag.

**Contaminated context.** When the dev agent reviews its own output, it
has seen the implementation rationale, the rejected alternatives, and
the conversation leading to each decision. It is structurally biased
toward confirming its own choices. A reviewer with a clean context --
seeing only the diff, the spec, and the audit output -- evaluates the
code as a future reader would, not as the author.

**No separation of concerns.** The dev agent has write access, tool
access, and conversational momentum. A review-only agent with read-only
file access (`-t file`) and no terminal cannot accidentally fix what it
finds, cannot be influenced by prior conversation, and cannot be
prompted into leniency by the author's explanations. It can only report.

**Single-model subagents don't solve this.** Claude Code's `Agent` tool
spawns subagents on Anthropic models only (Opus, Sonnet, Haiku). You
can't route a subagent to GPT-5.5 or Gemini. And even within Anthropic
models, all subagents share the same training lineage -- they disagree
on difficulty and detail level, not on fundamental reasoning patterns.
True model diversity requires crossing provider boundaries.

**No artifact awareness.** A code review in the same session sees the
diff but not the repo's signed manifests, artifact SHAs, or runtime
state. This pipeline runs a deterministic preflight audit that
machine-verifies manifest counts, feature dimensions, and artifact
integrity _before_ any LLM runs. Every reviewer gets this audit as
evidence, not inference. In our testing, this caught a 782-vs-662
feature count mismatch that three rounds of same-session review missed.

### What this pipeline adds

| Capability | Same-session review | Quick mode | Full mode |
|---|---|---|---|
| Clean context | no | yes | yes |
| Deterministic preflight | no | yes | yes |
| Artifact/manifest audit | no | yes | yes |
| Per-reviewer attribution | no | yes (2 lenses) | yes (4 lenses) |
| Model diversity | no | no (Anthropic only) | yes (cross-provider) |
| Write isolation | no | no | yes (`-t file` only) |
| Structured JSON schema | no | no | yes (jsonschema enforced) |
| Cost | free | free | ~$5-40 |
| Latency | instant | ~30s | ~5 min |
| Review packet persistence | no | no | yes |

Quick mode gives you 80% of the value (clean context + preflight +
spec-contract lens) at zero cost. Full mode adds cross-provider model
diversity for the remaining 20%.

## Architecture

```
Claude Code
  |
  |  "review this" (quick) or "full review" (full)
  v
ensemble-review agent (docs/ensemble-review.md)
  |
  |  1. git diff | scrub_diff.py  (secrets never touch disk)
  |  2. review_preflight.py       (deterministic audit)
  |  3. build context bundle      (docs, manifests, specs)
  |
  |  QUICK MODE:                         FULL MODE:
  |  Agent(opus, spec-contract)          hermes -z ... -m opus-4.6
  |  Agent(sonnet, correctness)          codex exec (or hermes -m haiku)
  |  (2 subagents, free)                 hermes -z ... -m sonnet-4.6
  |                                      hermes -z ... -m opus-4.6
  |                                      (4 sessions, paid)
  |
  |  4. synthesize convergence report
  v
User sees: preflight audit, blocking findings, convergence analysis
```

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

**Full mode** (paid): additionally requires:
1. [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed
2. AWS Bedrock authenticated. Optional: Codex CLI for GPT-5.5 (uses ChatGPT subscription, no API credits)

## Quick start

### 1. Copy the agent definition to your project

```bash
mkdir -p .claude/agents
cp docs/ensemble-review.md .claude/agents/ensemble-review.md
```

### 2. Copy the scripts

```bash
cp scripts/scrub_diff.py       your-project/scripts/
cp scripts/review_preflight.py your-project/scripts/
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
- "full review" or "full ensemble review" -- full mode (paid, 4 Hermes sessions)

## Files

| File | Purpose |
|---|---|
| `docs/ensemble-review.md` | Claude Code agent definition |
| `docs/ENSEMBLE_REVIEW.md` | Detailed usage guide and architecture |
| `docs/ensemble_review_result_schema.json` | Strict JSON schema for reviewer output |
| `scripts/scrub_diff.py` | Stdin credential scrubber (pipe, never writes raw to disk) |
| `scripts/review_preflight.py` | Deterministic pre-review audit |
| `scripts/validate_review_results.py` | JSON schema validator using `jsonschema` |
| `tests/test_review_scripts.py` | Test suite for all 3 scripts |

## Key design decisions

- **Claude Code orchestrates, not Hermes delegation**: Hermes `delegate_task`
  cannot route children to different models. Claude Code spawns 4 parallel
  `hermes -z` processes instead.
- **Secrets never touch disk**: `scrub_diff.py` reads from stdin via pipe.
  Exits non-zero on redaction, blocking the review until secrets are
  removed from the branch and rotated.
- **Evidence-based convergence**: a single finding with concrete artifact
  evidence is high-priority even if only one reviewer flags it. Inferred
  findings from one reviewer with no evidence are lower priority.
- **Strict schema**: `additionalProperties: false` at both top and finding
  levels. Validator uses `jsonschema.Draft202012Validator`.
- **Private temp directory**: `mktemp -d` instead of world-readable `/tmp/`.
- **Review packets persisted**: diff, context, audit, prompts, raw results,
  and report saved to `data/reviews/` for reproducibility.

## Customization

### Add/remove review lenses

Edit the agent definition to add a 5th reviewer or remove one. The JSON
schema accepts any string for the `reviewer` field -- no enum constraint.

### Change models

Update the `-m` and `--provider` flags in the agent's step 6. Verified
working combinations:

| Model | Hermes flags |
|---|---|
| Claude Opus 4.6 | `-m us.anthropic.claude-opus-4-6-v1 --provider bedrock` |
| Claude Opus 4.7 | `-m us.anthropic.claude-opus-4-7 --provider bedrock` |
| GPT-5.5 (via Codex CLI) | `codex exec -o result.json "prompt"` (auto-detected) |
| Claude Sonnet 4.6 | `-m us.anthropic.claude-sonnet-4-6 --provider bedrock` |
| Gemini 3.1 Pro | `-m google/gemini-3.1-pro-preview --provider nous` |
| Grok 4.3 | `-m x-ai/grok-4.3 --provider nous` |

### Add credential patterns

Edit `CREDENTIAL_PATTERNS` in `scripts/scrub_diff.py`. Current coverage:
API keys, passwords, tokens, private keys, AWS keys, OpenAI keys, GitHub
PATs, GitLab tokens, GCP service accounts, Azure connection strings.

## License

MIT
