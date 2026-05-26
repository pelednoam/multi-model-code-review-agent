# Multi-Model Code Review Agent

A review-only pipeline that runs multiple frontier LLMs in parallel
through [Hermes Agent](https://github.com/NousResearch/hermes-agent),
each with a different review lens, preceded by a deterministic
preflight audit. Designed for research and production codebases where
static diff review alone misses contract violations between code and
signed artifacts.

## Architecture

```
Claude Code (or any dev tool)
  |
  |  "run an ensemble review"
  v
ensemble-review agent (docs/ensemble-review.md)
  |
  |  1. git diff | scrub_diff.py  (secrets never touch disk)
  |  2. review_preflight.py       (deterministic audit)
  |  3. build context bundle      (docs, manifests, specs)
  |
  |-- hermes -z <security prompt>       -m opus-4.6    --> result-1.json
  |-- hermes -z <correctness prompt>    -m gpt-5.5     --> result-2.json
  |-- hermes -z <readability prompt>    -m sonnet-4.6  --> result-3.json
  |-- hermes -z <spec-contract prompt>  -m opus-4.6    --> result-4.json
  |   (all four run IN PARALLEL, 10min timeout each)
  |
  |  4. validate results against JSON schema
  |  5. persist review packet
  |  6. synthesize convergence report
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

1. **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** installed
2. **At least 2 LLM providers** authenticated (e.g. AWS Bedrock + OpenAI Codex)
3. **Python 3.11+** with `jsonschema` installed

Verify models work:

```bash
hermes -z "Say PONG" -m us.anthropic.claude-opus-4-6-v1 --provider bedrock --yolo
hermes -z "Say PONG" -m gpt-5.5 --provider openai-codex --yolo
hermes -z "Say PONG" -m us.anthropic.claude-sonnet-4-6 --provider bedrock --yolo
```

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
cp docs/ensemble_review_result_schema.json your-project/docs/review/
```

### 3. Configure for your project

Edit `scripts/review_preflight.py` to set:
- `SIGNED_MANIFESTS`: paths to your project's signed JSON artifacts
- `ARTIFACT_DIRS`: directories containing JSON artifacts to audit

### 4. Run from Claude Code

Say "run an ensemble review" or "ensemble review the current branch".

## Files

| File | Purpose |
|---|---|
| `docs/ensemble-review.md` | Claude Code agent definition |
| `docs/ENSEMBLE_REVIEW.md` | Detailed usage guide and architecture |
| `docs/ensemble_review_result_schema.json` | Strict JSON schema for reviewer output |
| `scripts/scrub_diff.py` | Stdin credential scrubber (pipe, never writes raw to disk) |
| `scripts/review_preflight.py` | Deterministic pre-review audit |
| `scripts/validate_review_results.py` | JSON schema validator using `jsonschema` |
| `tests/test_review_scripts.py` | 24 tests covering all 3 scripts |

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
| GPT-5.5 | `-m gpt-5.5 --provider openai-codex` |
| Claude Sonnet 4.6 | `-m us.anthropic.claude-sonnet-4-6 --provider bedrock` |
| Gemini 3.1 Pro | `-m google/gemini-3.1-pro-preview --provider nous` |
| Grok 4.3 | `-m x-ai/grok-4.3 --provider nous` |

### Add credential patterns

Edit `CREDENTIAL_PATTERNS` in `scripts/scrub_diff.py`. Current coverage:
API keys, passwords, tokens, private keys, AWS keys, OpenAI keys, GitHub
PATs, GitLab tokens, GCP service accounts, Azure connection strings.

## License

MIT
